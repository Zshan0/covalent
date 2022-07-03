# Copyright 2021 Agnostiq Inc.
#
# This file is part of Covalent.
#
# Licensed under the GNU Affero General Public License 3.0 (the "License").
# A copy of the License may be obtained with this software package or at
#
#      https://www.gnu.org/licenses/agpl-3.0.en.html
#
# Use of this file is prohibited except in compliance with the License. Any
# modifications or derivative works of this file must retain this copyright
# notice, and modified files must contain a notice indicating that they have
# been altered from the originals.
#
# Covalent is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the License for more details.
#
# Relief from the License may be granted by purchasing a commercial license.

"""
Defines the core functionality of the dispatcher
"""

import json
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Dict, List

import cloudpickle as pickle

from covalent import dispatch, dispatch_sync
from covalent._results_manager import Result
from covalent._results_manager import results_manager as rm
from covalent._shared_files import logger
from covalent._shared_files.context_managers import active_lattice_manager
from covalent._shared_files.defaults import (
    attr_prefix,
    electron_dict_prefix,
    electron_list_prefix,
    generator_prefix,
    parameter_prefix,
    prefix_separator,
    sublattice_prefix,
    subscript_prefix,
)
from covalent._workflow import DepsBash, DepsCall, DepsPip
from covalent._workflow.lattice import Lattice
from covalent._workflow.transport import TransportableObject
from covalent.executor import _executor_manager
from covalent_ui import result_webhook

from .._db.dispatchdb import DispatchDB
from ..entry_point import futures

app_log = logger.app_log
log_stack_info = logger.log_stack_info


# This is to be run out-of-process
def _dispatch(fn, *args, **kwargs):
    dispatch_id = dispatch(fn)(*args, **kwargs)
    return dispatch_id


def generate_node_result(
    node_id,
    start_time=None,
    end_time=None,
    status=None,
    output=None,
    error=None,
    stdout=None,
    stderr=None,
    sublattice_result=None,
):

    return {
        "node_id": node_id,
        "start_time": start_time,
        "end_time": end_time,
        "status": status,
        "output": output,
        "error": error,
        "stdout": stdout,
        "stderr": stderr,
        "sublattice_result": sublattice_result,
    }


def _get_task_inputs(node_id: int, node_name: str, result_object: Result) -> dict:
    """
    Return the required inputs for a task execution.
    This makes sure that any node with child nodes isn't executed twice and fetches the
    result of parent node to use as input for the child node.

    Args:
        node_id: Node id of this task in the transport graph.
        node_name: Name of the node.
        result_object: Result object to be used to update and store execution related
                       info including the results.

    Returns:
        inputs: Input dictionary to be passed to the task containing args, kwargs,
                and any parent node execution results if present.
    """

    if node_name.startswith(electron_list_prefix):
        values = [
            result_object.lattice.transport_graph.get_node_value(parent, "output")
            for parent in result_object.lattice.transport_graph.get_dependencies(node_id)
        ]
        task_input = {"args": [], "kwargs": {"x": TransportableObject.make_transportable(values)}}
    elif node_name.startswith(electron_dict_prefix):
        values = {}
        for parent in result_object.lattice.transport_graph.get_dependencies(node_id):

            edge_data = result_object.lattice.transport_graph.get_edge_data(parent, node_id)

            value = result_object.lattice.transport_graph.get_node_value(parent, "output")
            for e_key, d in edge_data.items():
                key = d["edge_name"]
                values[key] = value

        task_input = {"args": [], "kwargs": {"x": TransportableObject.make_transportable(values)}}
    else:
        task_input = {"args": [], "kwargs": {}}

        for parent in result_object.lattice.transport_graph.get_dependencies(node_id):

            edge_data = result_object.lattice.transport_graph.get_edge_data(parent, node_id)
            value = result_object.lattice.transport_graph.get_node_value(parent, "output")

            for e_key, d in edge_data.items():
                if d["param_type"] == "arg":
                    task_input["args"].append(value)
                elif d["param_type"] == "kwarg":
                    key = d["edge_name"]
                    task_input["kwargs"][key] = value

    return task_input


# This is to be run out-of-process
def _post_process(lattice: Lattice, node_outputs: Dict, execution_order: List[List]) -> Any:
    """
    Post processing function to be called after the lattice execution.
    This takes care of executing statements that were not an electron
    but were inside the lattice's function. It also replaces any calls
    to an electron with the result of that electron execution, hence
    preventing a local execution of electron's function.

    Note: Here `node_outputs` is used instead of `electron_outputs`
    since an electron can be called multiple times with possibly different
    arguments, but every time it's called, it will be executed as a separate node.
    Thus, output of every node is used.

    Args:
        lattice: Lattice object that was dispatched.
        node_outputs: Dictionary containing the output of all the nodes.
        execution_order: List of lists containing the order of execution of the nodes.

    Reurns:
        result: The result of the lattice function.
    """

    ordered_node_outputs = [
        val
        for key, val in node_outputs.items()
        if not key.startswith(prefix_separator) or key.startswith(sublattice_prefix)
    ]

    with active_lattice_manager.claim(lattice):
        lattice.post_processing = True
        lattice.electron_outputs = ordered_node_outputs
        args = []
        kwargs = {}
        for arg in lattice.args:
            args.append(arg.get_deserialized())
        for k, v in lattice.kwargs.items():
            kwargs[k] = v.get_deserialized()
        workflow_function = lattice.workflow_function.get_deserialized()
        result = workflow_function(*args, **kwargs)
        lattice.post_processing = False
        return result


def _run_task(
    node_id: int,
    dispatch_id: str,
    results_dir: str,
    inputs: Dict,
    serialized_callable: Any,
    selected_executor: Any,
    call_before: List,
    call_after: List,
    node_name: str,
    tasks_pool: ThreadPoolExecutor,
    workflow_executor: Any,
) -> None:
    """
    Run a task with given inputs on the selected executor.
    Also updates the status of current node execution while
    checking if a redispatch has occurred. Exclude those nodes
    from execution which were completed.

    Also verifies if execution of this dispatch has been cancelled.

    Args:
        inputs: Inputs for the task.
        result_object: Result object being used for current dispatch
        node_id: Node id of the task to be executed.

    Returns:
        None
    """

    # Instantiate the executor from JSON
    try:
        short_name, object_dict = selected_executor

        app_log.debug(f"Running task {node_name} using executor {short_name}, {object_dict}")

        # the executor is determined during scheduling and provided in the execution metadata
        executor = _executor_manager.get_executor(short_name)
        executor.from_dict(object_dict)
    except Exception as ex:
        app_log.debug(f"Exception when trying to determine executor: {ex}")
        raise ex

    # run the task on the executor and register any failures
    try:

        if node_name.startswith(sublattice_prefix):

            try:
                short_name, object_dict = workflow_executor

                if short_name == "client":
                    raise RuntimeError("No executor selected for dispatching sublattices")

            except Exception as ex:
                app_log.debug(f"Exception when trying to determine sublattice executor: {ex}")
                raise ex

            sub_dispatch_inputs = {"args": [serialized_callable], "kwargs": inputs["kwargs"]}
            for arg in inputs["args"]:
                sub_dispatch_inputs["args"].append(arg)

            # Dispatch the sublattice workflow. This must be run
            # externally since it involves deserializing the
            # sublattice workflow function.
            fut = tasks_pool.submit(
                _run_task,
                node_id=-1,
                dispatch_id=dispatch_id,
                results_dir=results_dir,
                serialized_callable=TransportableObject.make_transportable(_dispatch),
                selected_executor=workflow_executor,
                node_name="dispatch_sublattice",
                call_before=[],
                call_after=[],
                inputs=sub_dispatch_inputs,
                tasks_pool=tasks_pool,
                workflow_executor=workflow_executor,
            )

            sub_dispatch_id = json.loads(fut.result()["output"].json)

            app_log.debug(f"Sublattice dispatch id: {sub_dispatch_id}")

            # Read the result object directly from the server

            sublattice_result = futures[sub_dispatch_id].result()

            if not sublattice_result:
                raise RuntimeError("Sublattice execution failed")

            output = sublattice_result.encoded_result

            end_time = datetime.now(timezone.utc)

            node_result = generate_node_result(
                node_id=node_id,
                end_time=end_time,
                status=Result.COMPLETED,
                output=output,
                sublattice_result=sublattice_result,
            )

            # Don't continue unless sublattice finishes
            if sublattice_result.status != Result.COMPLETED:
                node_result["status"] = Result.FAILED
                node_result["error"] = "Sublattice workflow failed to complete"

        else:
            app_log.debug(f"Executing task {node_name}")
            output, stdout, stderr = executor.execute(
                function=serialized_callable,
                args=inputs["args"],
                kwargs=inputs["kwargs"],
                call_before=call_before,
                call_after=call_after,
                dispatch_id=dispatch_id,
                results_dir=results_dir,
                node_id=node_id,
            )

            end_time = datetime.now(timezone.utc)

            node_result = generate_node_result(
                node_id=node_id,
                end_time=end_time,
                status=Result.COMPLETED,
                output=output,
                stdout=stdout,
                stderr=stderr,
            )

    except Exception as ex:
        end_time = datetime.now(timezone.utc)

        app_log.error(f"Exception occurred when running task {node_id}: {ex}")

        node_result = generate_node_result(
            node_id=node_id,
            end_time=end_time,
            status=Result.FAILED,
            error="".join(traceback.TracebackException.from_exception(ex).format()),
        )

    return node_result


def _run_planned_workflow(result_object: Result, thread_pool: ThreadPoolExecutor) -> Result:
    """
    Run the workflow in the topological order of their position on the
    transport graph. Does this in an asynchronous manner so that nodes
    at the same level are executed in parallel. Also updates the status
    of the whole workflow execution.

    Args:
        result_object: Result object being used for current dispatch

    Returns:
        None
    """

    def update_node_result(node_result: dict):

        result_object._update_node(**node_result)
        with DispatchDB() as db:
            db.upsert(result_object.dispatch_id, result_object)
        result_object.save()
        result_webhook.send_update(result_object)

    def task_callback(future: Future):
        node_result = future.result()
        update_node_result(node_result)

    app_log.debug(f"Running workflow {result_object.dispatch_id}")
    result_object._status = Result.RUNNING
    result_object._start_time = datetime.now(timezone.utc)

    # Executor for post_processing and dispatching sublattices
    pp_executor = result_object.lattice.get_metadata("workflow_executor")
    pp_executor_data = result_object.lattice.get_metadata("workflow_executor_data")
    post_processor = [pp_executor, pp_executor_data]

    order = result_object.lattice.transport_graph.get_topologically_sorted_graph()

    for nodes in order:
        futures: list = []

        for node_id in nodes:
            # Get name of the node for the current task
            node_name = result_object.lattice.transport_graph.get_node_value(node_id, "name")

            if node_name.startswith(
                (subscript_prefix, generator_prefix, parameter_prefix, attr_prefix)
            ):
                if node_name.startswith(parameter_prefix):
                    output = result_object.lattice.transport_graph.get_node_value(node_id, "value")
                else:
                    parent = result_object.lattice.transport_graph.get_dependencies(node_id)[0]
                    output = result_object.lattice.transport_graph.get_node_value(parent, "output")

                    if node_name.startswith(attr_prefix):
                        attr = result_object.lattice.transport_graph.get_node_value(
                            node_id, "attribute_name"
                        )
                        output = getattr(output, attr)
                    else:
                        key = result_object.lattice.transport_graph.get_node_value(node_id, "key")
                        output = output[key]

                result_object._update_node(
                    node_id=node_id,
                    start_time=datetime.now(timezone.utc),
                    end_time=datetime.now(timezone.utc),
                    status=Result.COMPLETED,
                    output=output,
                )

                continue

            app_log.debug(f"Gathering inputs for task {node_id}")
            task_input = _get_task_inputs(node_id, node_name, result_object)

            start_time = datetime.now(timezone.utc)
            serialized_callable = result_object.lattice.transport_graph.get_node_value(
                node_id, "function"
            )

            try:
                selected_executor = result_object.lattice.transport_graph.get_node_value(
                    node_id, "metadata"
                )["executor"]

                selected_executor_data = result_object.lattice.transport_graph.get_node_value(
                    node_id, "metadata"
                )["executor_data"]
            except Exception as ex:
                app_log.error(f"Exception when trying to extract executor: {ex}")
                raise ex

            app_log.debug(f"Collecting deps for task {node_id}")
            try:
                deps = result_object.lattice.transport_graph.get_node_value(node_id, "metadata")[
                    "deps"
                ]

                # Assemble call_before and call_after from all the deps

                call_before_objs_json = result_object.lattice.transport_graph.get_node_value(
                    node_id, "metadata"
                )["call_before"]
                call_after_objs_json = result_object.lattice.transport_graph.get_node_value(
                    node_id, "metadata"
                )["call_after"]

                call_before = []
                call_after = []

                # Rehydrate deps from JSON
                if "bash" in deps:
                    dep = DepsBash()
                    dep.from_dict(deps["bash"])
                    call_before.append(dep.apply())

                if "pip" in deps:
                    dep = DepsPip()
                    dep.from_dict(deps["pip"])
                    call_before.append(dep.apply())

                for dep_json in call_before_objs_json:
                    dep = DepsCall()
                    dep.from_dict(dep_json)
                    call_before.append(dep.apply())

                for dep_json in call_after_objs_json:
                    dep = DepsCall()
                    dep.from_dict(dep_json)
                    call_after.append(dep.apply())

            except Exception as ex:
                app_log.error(f"Exception when trying to collect deps: {ex}")
                raise ex

            try:
                update_node_result(
                    generate_node_result(
                        node_id=node_id,
                        start_time=start_time,
                        status=Result.RUNNING,
                    )
                )
            except Exception as ex:
                app_log.error(f"Error updating node {node_id}: {ex}")
                raise ex

            app_log.debug(f"Submitting task {node_id} to executor")

            # Add the task generated for the node to the list of tasks
            future = thread_pool.submit(
                _run_task,
                node_id=node_id,
                dispatch_id=result_object.dispatch_id,
                results_dir=result_object.results_dir,
                serialized_callable=serialized_callable,
                selected_executor=[selected_executor, selected_executor_data],
                node_name=node_name,
                call_before=call_before,
                call_after=call_after,
                inputs=task_input,
                tasks_pool=thread_pool,
                workflow_executor=post_processor,
            )

            future.add_done_callback(task_callback)

            futures.append(future)

        # run the tasks for the current iteration concurrently
        # results are not used right now, but can be in the case of multiprocessing
        wait(futures)
        # del futures

        # When one or more nodes failed in the last iteration, don't iterate further
        for node_id in nodes:
            if result_object._get_node_status(node_id) == Result.FAILED:
                result_object._status = Result.FAILED
                result_object._end_time = datetime.now(timezone.utc)
                result_object._error = f"Node {result_object._get_node_name(node_id)} failed: \n{result_object._get_node_error(node_id)}"
                with DispatchDB() as db:
                    db.upsert(result_object.dispatch_id, result_object)
                result_object.save()
                result_webhook.send_update(result_object)
                return result_object

            elif result_object._get_node_status(node_id) == Result.CANCELLED:
                result_object._status = Result.CANCELLED
                result_object._end_time = datetime.now(timezone.utc)
                with DispatchDB() as db:
                    db.upsert(result_object.dispatch_id, result_object)
                result_object.save()
                result_webhook.send_update(result_object)
                return result_object

    # post process the lattice

    result_object._status = Result.POSTPROCESSING

    app_log.debug(f"Preparing to post-process workflow {result_object.dispatch_id}")

    if pp_executor == "client":
        app_log.debug("Workflow to be postprocessed client side")
        result_object._status = Result.PENDING_POSTPROCESSING
        result_object._end_time = datetime.now(timezone.utc)
        with DispatchDB() as db:
            db.upsert(result_object.dispatch_id, result_object)
        result_object.save()
        result_webhook.send_update(result_object)
        return result_object

    post_processing_inputs = {}
    post_processing_inputs["args"] = [
        TransportableObject.make_transportable(result_object.lattice),
        TransportableObject.make_transportable(result_object.get_all_node_outputs()),
        TransportableObject.make_transportable(order),
    ]
    post_processing_inputs["kwargs"] = {}

    try:
        future = thread_pool.submit(
            _run_task,
            node_id=-1,
            dispatch_id=result_object.dispatch_id,
            results_dir=result_object.results_dir,
            serialized_callable=TransportableObject(_post_process),
            selected_executor=post_processor,
            node_name="post_process",
            call_before=[],
            call_after=[],
            inputs=post_processing_inputs,
            tasks_pool=thread_pool,
            workflow_executor=post_processor,
        )
        pp_start_time = datetime.now(timezone.utc)
        app_log.debug(
            f"Submitted post-processing job to executor {post_processor} at {pp_start_time}"
        )

        post_process_result = future.result()
    except Exception as ex:
        app_log.debug("Exception during post-processing: {ex}")
        result_object._status = Result.POSTPROCESSING_FAILED
        result_object._error = "Post-processing failed"
        result_object._end_time = datetime.now(timezone.utc)
        with DispatchDB() as db:
            db.upsert(result_object.dispatch_id, result_object)
        result_object.save()
        result_webhook.send_update(result_object)

        return result_object

    # app_log.debug(f"Post-process result: {post_process_result}")

    if post_process_result["status"] != Result.COMPLETED:
        err = post_process_result["stderr"]
        app_log.debug(f"Post-processing failed: {err}")
        result_object._status = Result.POSTPROCESSING_FAILED
        result_object._error = f"Post-processing failed: {err}"
        result_object._end_time = datetime.now(timezone.utc)
        with DispatchDB() as db:
            db.upsert(result_object.dispatch_id, result_object)
        result_object.save()
        result_webhook.send_update(result_object)

        return result_object

    pp_end_time = post_process_result["end_time"]
    app_log.debug(f"Post-processing completed at {pp_end_time}")

    result_object._result = post_process_result["output"]

    # app_log.debug(f"Result: {result_object._result}")

    result_object._status = Result.COMPLETED
    result_object._end_time = datetime.now(timezone.utc)
    with DispatchDB() as db:
        db.upsert(result_object.dispatch_id, result_object)
    result_object.save(write_source=True)
    result_webhook.send_update(result_object)

    return result_object


def _plan_workflow(result_object: Result) -> None:
    """
    Function to plan a workflow according to a schedule.
    Planning means to decide which executors (along with their arguments) will
    be used by each node.

    Args:
        result_object: Result object being used for current dispatch

    Returns:
        None
    """

    if result_object.lattice.get_metadata("schedule"):
        # Custom scheduling logic of the format:
        # scheduled_executors = get_schedule(result_object)

        # for node_id, executor in scheduled_executors.items():
        #    result_object.lattice.transport_graph.set_node_value(node_id, "executor", executor)
        pass


def run_workflow(dispatch_id: str, json_lattice: str, tasks_pool: ThreadPoolExecutor) -> Result:
    """
    Plan and run the workflow by loading the result object corresponding to the
    dispatch id and retrieving essential information from it.
    Returns without changing anything if a redispatch is done of a (partially or fully)
    completed workflow with the same dispatch id.

    Args:
        dispatch_id: Dispatch id of the workflow to be run
        results_dir: Directory where the result object is stored

    Returns:
        The result object from the workflow execution
    """
    lattice = Lattice.deserialize_from_json(json_lattice)
    result_object = Result(lattice, lattice.metadata["results_dir"])

    result_object._dispatch_id = dispatch_id

    # transport_graph = _TransportGraph()
    # transport_graph.deserialize(result_object.lattice.transport_graph)
    # result_object._lattice.transport_graph = transport_graph

    result_object._initialize_nodes()

    result_object.save()

    if result_object.status == Result.COMPLETED:
        return result_object

    try:
        _plan_workflow(result_object)
        result_object = _run_planned_workflow(result_object, tasks_pool)

    except Exception as ex:
        result_object._status = Result.FAILED
        result_object._end_time = datetime.now(timezone.utc)
        result_object._error = "".join(traceback.TracebackException.from_exception(ex).format())
        with DispatchDB() as db:
            db.upsert(result_object.dispatch_id, result_object)
        result_object.save()

    return result_object


def cancel_workflow(dispatch_id: str) -> None:
    """
    Cancels a dispatched workflow using publish subscribe mechanism
    provided by Dask.

    Args:
        dispatch_id: Dispatch id of the workflow to be cancelled

    Returns:
        None
    """

    # shared_var = Variable(dispatch_id)
    # shared_var.set(str(Result.CANCELLED))
    pass
