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
import json
import math
import os
import pathlib
import time
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

import pydash


@dataclass
class Status:
    STATUS: str

    def __bool__(self):
        """
        Return True if the status is not "NEW_OBJECT"
        """

        return self.STATUS != "NEW_OBJECT"

    def __str__(self) -> str:
        return self.STATUS


class RESULT_STATUS:
    NEW_OBJECT = Status("NEW_OBJECT")
    COMPLETED = Status("COMPLETED")
    FAILED = Status("FAILED")
    RUNNING = Status("RUNNING")
    CANCELLED = Status("CANCELLED")


class DispatchInfo(NamedTuple):
    """
    Information about a dispatch to be shared to a task post dispatch.

    Attributes:
        dispatch_id: Dispatch id of the dispatch.
    """

    dispatch_id: str


class TimerError(Exception):
    """A custom exception used to report errors in usage of the Timer class"""


class Timer:
    """
    Information on execution times of the API function calls when a workflow is dispatched

    Attributes:
        endpoint: The name of an endpoint or function call
        descriptor : A brief description of an action e.g., Updating a workflow
        service: The name of the service e.g. DISPATCHER,
        dispatch_id: The uuid of a dispatch
    """

    DATA = "Data Service"
    DISPATCHER = "Dispatcher Service"
    QUEUER = "Queuer Service"
    QUEUE_CONSUMER = "Queue Consumer Service"
    RESULTS = "Results Service"
    RUNNER = "Runner Service"
    UI = "UI Service"
    NATS_SERVER = "Nats Service"

    def write_to_file(self):
        # json_file_location = '/Users/alejandro/Documents/agnostiq/covalent/benchmarking/src/experiment_results.json'
        json_file_location = (
            os.path.dirname(os.path.abspath(__file__))
            + "/../../benchmarking/src/experiment_results.json"
        )
        report_json = {}

        if not os.path.exists(json_file_location):
            with open(json_file_location, "w+") as outfile:
                json.dump(report_json, outfile)

        with open(json_file_location, "r") as outfile:
            report_json = json.loads(outfile.read())

        with open(json_file_location, "w") as outfile:
            normalized_desc = self.descriptor.lower().replace(" ", "_")
            value_dict = {
                "label": self.descriptor,
                "value": self.elapsed_time,
                "endpoint": self.endpoint,
                "service": self.service,
            }
            nested_dict_path = f"{self.dispatch_id}.{str(math.trunc(self.end_timestamp))}__{self.endpoint}__{normalized_desc}"
            pydash.set_(report_json, nested_dict_path, value_dict)
            json.dump(report_json, outfile, indent=4)

    def __init__(self, endpoint: str, descriptor: str, service: str, dispatch_id=None):
        self._start_time = None
        self.endpoint = endpoint
        self.descriptor = descriptor
        self.service = service
        self.dispatch_id = dispatch_id

    def start(self):
        """Start a new timer"""
        if self._start_time is not None:
            self._start_time = None

        self._start_time = time.perf_counter()
        debug_message = (
            f" Dispatch id: {self.dispatch_id} \nMetadata: {self.descriptor}  was initiated by {self.endpoint} in {self.service} at "
            f"{datetime.now()} "
        )
        print(debug_message)

        return self

    def stop(self):
        """Stop the timer, and report the elapsed time"""
        if self._start_time is None:
            print(
                f"Timer for service {self.service}, endpoint: {self.endpoint}, and descriptor {self.descriptor} was never started."
            )
        else:
            self.end_timestamp = time.time()
            self.elapsed_time = time.perf_counter() - self._start_time
            self._start_time = None
            debug_message = (
                f" Dispatch id: {self.dispatch_id} \nMetadata: {self.descriptor} was initiated by {self.endpoint} "
                f" in {self.service} and ran in {self.elapsed_time:0.4f} ms "
            )
            print(debug_message)
        self.write_to_file()
