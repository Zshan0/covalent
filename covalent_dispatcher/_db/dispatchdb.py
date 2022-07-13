import json
import os
import sqlite3
from datetime import datetime
from typing import List, Tuple

import networkx as nx
import simplejson

import covalent.executor as covalent_executor
from covalent._data_store import DataStore
from covalent._results_manager.result import Result
from covalent._shared_files import logger
from covalent._shared_files.config import get_config
from covalent._shared_files.util_classes import Status
from covalent._shared_files.utils import get_named_params

app_log = logger.app_log
log_stack_info = logger.log_stack_info
# DB Schema:
# TABLE dispatches
# * dispatch_id text primary key
# * result_dict text ; json-serialied dictionary representation of Result

# TODO: Move these to a common utils module


def extract_graph_node(node):
    # avoid mutating original node
    node = node.copy()

    # doc string
    f = node.get("function")
    if f is not None:
        node["doc"] = f.get_deserialized().__doc__

    # metadata
    node["metadata"] = extract_metadata(node["metadata"])

    # prevent JSON encoding
    node["kwargs"] = encode_dict(node.get("kwargs"))

    # remove unused fields
    node.pop("function", None)
    node.pop("node_name", None)

    return node


def extract_metadata(metadata: dict):
    try:
        # avoid mutating original metadata
        metadata = metadata.copy()

        name = metadata["executor"]
        executor = covalent_executor._executor_manager.get_executor(name=name)

        if executor is not None:
            # extract attributes
            metadata["executor"] = encode_dict(executor.__dict__)
            if isinstance(name, str):
                metadata["executor_name"] = name
            else:
                metadata["executor_name"] = f"<{executor.__class__.__name__}>"

        metadata["deps"] = encode_dict(metadata["deps"])
        call_before = metadata["call_before"]
        call_after = metadata["call_after"]
        for i, dep in enumerate(call_before):
            call_before[i] = str(dep)

        for i, dep in enumerate(call_after):
            call_after[i] = str(dep)

        metadata["call_before"] = call_before
        metadata["call_after"] = call_after

    except (KeyError, AttributeError):
        pass

    return metadata


def encode_dict(d):
    """Avoid JSON encoding when python str() suffices"""
    if not isinstance(d, dict):
        return d
    return {k: str(v) for (k, v) in d.items()}


def extract_graph(graph):
    graph = nx.json_graph.node_link_data(graph)
    nodes = list(map(extract_graph_node, graph["nodes"]))
    return {
        "nodes": nodes,
        "links": graph["links"],
    }


def result_encoder(obj):
    if isinstance(obj, Status):
        return obj.STATUS
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def encode_result(result_obj):
    lattice = result_obj.lattice
    ((named_args, named_kwargs),) = (
        get_named_params(lattice.workflow_function, lattice.args, lattice.kwargs),
    )
    result_dict = {
        "dispatch_id": result_obj.dispatch_id,
        "status": result_obj.status,
        "result": result_obj.result,
        "start_time": result_obj.start_time,
        "end_time": result_obj.end_time,
        "results_dir": result_obj.results_dir,
        "error": result_obj.error,
        "lattice": {
            "function_string": lattice.workflow_function_string,
            "doc": lattice.__doc__,
            "name": lattice.__name__,
            "inputs": encode_dict({**named_args, **named_kwargs}),
            "metadata": extract_metadata(lattice.metadata),
        },
        "graph": extract_graph(result_obj.lattice.transport_graph._graph),
    }

    jsonified_result = simplejson.dumps(result_dict, default=result_encoder, ignore_nan=True)

    return jsonified_result


class DispatchDB:
    """
    Wrapper for the database of workflows.
    """

    def __init__(self, dbpath: str = None) -> None:
        if dbpath:
            self._dbpath = dbpath
        else:
            self._dbpath = get_config("user_interface.dispatch_db")

    def _get_data_store(self) -> DataStore:
        """Return the DataStore instance to write records."""

        return DataStore(db_URL=f"sqlite+pysqlite:///{self._dbpath}", initialize_db=True)

    def save_db(self, result_object: Result):

        try:
            # set echo=True only if covalent is started in debug /develop mode `covalent start -d`
            # `initialize_db` flag can be removed as its redundant (sqlalchemy does check if the tables are
            # created or not before inserting/updating data)
            result_object.persist(self._get_data_store())
        except Exception as e:
            app_log.exception(f"Exception occured while saving to DB: {e}.")
            raise
