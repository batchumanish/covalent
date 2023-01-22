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
Module for defining a Dask executor that submits the input python function in a dask cluster
and waits for execution to finish then returns the result.

This is a plugin executor module; it is loaded if found and properly structured.
"""

import os
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Callable, Dict, List, Tuple

import cloudpickle as pickle
from dask.distributed import Client, Future

from covalent._shared_files import TaskRuntimeError, logger

# Relative imports are not allowed in executor plugins
from covalent._shared_files.config import get_config
from covalent._shared_files.util_classes import RESULT_STATUS
from covalent._workflow.depsbash import DepsBash
from covalent._workflow.depscall import DepsCall
from covalent._workflow.depspip import DepsPip
from covalent.executor.base import AsyncBaseExecutor, wrapper_fn
from covalent.executor.utils.wrappers import io_wrapper as dask_wrapper

# The plugin class name must be given by the executor_plugin_name attribute:
EXECUTOR_PLUGIN_NAME = "ManagedDaskExecutor"

app_log = logger.app_log
log_stack_info = logger.log_stack_info

_EXECUTOR_PLUGIN_DEFAULTS = {
    "log_stdout": "stdout.log",
    "log_stderr": "stderr.log",
    "cache_dir": os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.join(os.environ["HOME"], ".cache"), "covalent"
    ),
}


_address_client_mapper = {}

# See https://github.com/dask/distributed/issues/5667
_clients = {}

# See
# https://stackoverflow.com/questions/62164283/why-do-my-dask-futures-get-stuck-in-pending-and-never-finish
_futures = {}


# Copied from runner.py
def _gather_deps(deps, call_before_objs_json, call_after_objs_json) -> Tuple[List, List]:
    """Assemble deps for a node into the final call_before and call_after"""

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

    return call_before, call_after


# URIs are just file paths
def run_task_from_uris(
    function_uri: str,
    deps_uris: str,
    call_before_uris: str,
    call_after_uris: str,
    args_uris: str,
    kwargs_uris: str,
    output_uri: str,
    stdout_uri: str,
    stderr_uri: str,
):

    prefix = "file://"
    prefix_len = len(prefix)

    with open(stdout_uri, "w") as stdout, open(stderr_uri, "w") as stderr:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                if function_uri.startswith(prefix):
                    function_uri = function_uri[prefix_len:]

                with open(function_uri, "rb") as f:
                    serialized_fn = pickle.load(f)

                ser_args = []
                ser_kwargs = {}
                for uri in args_uris:
                    if uri.startswith(prefix):
                        uri = uri[prefix_len:]
                    with open(uri, "rb") as f:
                        ser_args.append(pickle.load(f))

                for key, uri in kwargs_uris.items():
                    if uri.startswith(prefix):
                        uri = uri[prefix_len:]
                    with open(uri, "rb") as f:
                        ser_kwargs[key] = pickle.load(f)

                if deps_uris.startswith(prefix):
                    deps_uris = deps_uris[prefix_len:]
                with open(deps_uris, "rb") as f:
                    deps_json = pickle.load(f)

                if call_before_uris.startswith(prefix):
                    call_before_uris = call_before_uris[prefix_len:]
                with open(call_before_uris, "rb") as f:
                    call_before_json = pickle.load(f)

                if call_after_uris.startswith(prefix):
                    call_after_uris = call_after_uris[prefix_len:]
                with open(call_after_uris, "rb") as f:
                    call_after_json = pickle.load(f)

                call_before, call_after = _gather_deps(
                    deps_json, call_before_json, call_after_json
                )

                exception_occurred = False

                ser_output = wrapper_fn(
                    serialized_fn, call_before, call_after, *ser_args, **ser_kwargs
                )

                with open(output_uri, "wb") as f:
                    pickle.dump(ser_output, f)

            except Exception as ex:
                exception_occurred = True
                tb = "".join(traceback.TracebackException.from_exception(ex).format())
                print(tb, file=sys.stderr)
                output_uri = None

    return output_uri, stdout_uri, stderr_uri, exception_occurred


class ManagedDaskExecutor(AsyncBaseExecutor):
    """
    Dask executor class that submits the input function to a running LOCAL dask cluster.
    """

    SUPPORTS_MANAGED_EXECUTION = True

    def __init__(
        self,
        scheduler_address: str = "",
        log_stdout: str = "stdout.log",
        log_stderr: str = "stderr.log",
        cache_dir: str = "",
        current_env_on_conda_fail: bool = False,
        time_limit: int = 600,
    ) -> None:
        if not cache_dir:
            cache_dir = os.path.join(
                os.environ.get("XDG_CACHE_HOME") or os.path.join(os.environ["HOME"], ".cache"),
                "covalent",
            )

        if not scheduler_address:
            try:
                scheduler_address = get_config("dask.scheduler_address")
            except KeyError as ex:
                app_log.debug(
                    "No dask scheduler address found in config. Address must be set manually."
                )

        super().__init__(log_stdout, log_stderr, cache_dir=cache_dir)

        self.scheduler_address = scheduler_address
        self.time_limit = time_limit

    async def run(self, function: Callable, args: List, kwargs: Dict, task_metadata: Dict):
        """Submit the function and inputs to the dask cluster"""

        node_id = task_metadata["node_id"]

        dask_client = _address_client_mapper.get(self.scheduler_address)

        if not dask_client:
            dask_client = Client(address=self.scheduler_address, asynchronous=True)
            _address_client_mapper[self.scheduler_address] = dask_client

            await dask_client

        future = dask_client.submit(dask_wrapper, function, args, kwargs)
        app_log.debug(f"Submitted task {node_id} to dask")

        result, worker_stdout, worker_stderr, tb = await future

        print(worker_stdout, end="", file=self.task_stdout)
        print(worker_stderr, end="", file=self.task_stderr)

        if tb:
            print(tb, end="", file=self.task_stderr)
            raise TaskRuntimeError(tb)

        # FIX: need to get stdout and stderr from dask worker and print them
        return result

    async def send(
        self,
        function_uri: str,
        deps_uris: str,
        call_before_uris: str,
        call_after_uris: str,
        args_uris: str,
        kwargs_uris: str,
        task_metadata: dict,
    ):
        # Assets are assumed to be accessible by the compute backend
        # at the provided URIs

        # The Asset Manager is responsible for uploading all assets
        # Returns a job handle (should be JSONable)

        dask_client = Client(address=self.scheduler_address, asynchronous=True)
        await dask_client

        node_id = task_metadata["node_id"]
        dispatch_id = task_metadata["dispatch_id"]

        output_uri = os.path.join(self.cache_dir, f"result_{dispatch_id}-{node_id}.pkl")
        stdout_uri = os.path.join(self.cache_dir, f"stdout_{dispatch_id}-{node_id}.txt")
        stderr_uri = os.path.join(self.cache_dir, f"stderr_{dispatch_id}-{node_id}.txt")
        # future = dask_client.submit(lambda x: x**3, 3)

        key = f"dask_job_{dispatch_id}:{node_id}"
        future = dask_client.submit(
            run_task_from_uris,
            function_uri=function_uri,
            deps_uris=deps_uris,
            call_before_uris=call_before_uris,
            call_after_uris=call_after_uris,
            args_uris=args_uris,
            kwargs_uris=kwargs_uris,
            output_uri=output_uri,
            stdout_uri=stdout_uri,
            stderr_uri=stderr_uri,
            key=key,
        )

        _clients[key] = dask_client
        app_log.debug(f"Dask client {id(dask_client)}")
        app_log.debug(f"Submitted task {dispatch_id}:{node_id}, key {future.key}")

        _futures[key] = future
        app_log.debug(f"Retaining a reference to the future {dispatch_id}:{node_id}")

        await future
        return future.key

    async def poll(self, task_metadata: Dict, job_handle: Any):
        dispatch_id = task_metadata["dispatch_id"]
        node_id = task_metadata["node_id"]

        app_log.debug(f"Waiting for task {dispatch_id}:{node_id}, key {job_handle}")

        dask_client = _clients[job_handle]
        app_log.debug(f"Dask client {id(dask_client)}")

        fut = Future(key=job_handle, client=dask_client)
        app_log.debug(f"Future {fut}")
        try:
            await fut.result(timeout=self.time_limit)

            app_log.debug(f"ManagedDask: task {dispatch_id}:{node_id} finished")
            app_log.debug(f"Future {fut}")
        except Exception as ex:
            app_log.debug(f"Exception while polling: {ex}")
            app_log.debug(f"Future {fut}")
        return 0

    async def receive(self, task_metadata: Dict, job_handle: Any):

        # Returns (output_uri, stdout_uri, stderr_uri,
        # exception_raised)

        # Job should have reached a terminal state by the time this is invoked.
        dispatch_id = task_metadata["dispatch_id"]
        node_id = task_metadata["node_id"]
        dask_client = _clients.pop(job_handle)

        fut = Future(key=job_handle, client=dask_client)
        fut = _futures.pop(job_handle)

        # Result should be ready to read immediately

        output_uri, stdout_uri, stderr_uri, exception_raised = await fut.result(1)

        terminal_status = RESULT_STATUS.FAILED if exception_raised else RESULT_STATUS.COMPLETED

        return output_uri, stdout_uri, stderr_uri, terminal_status

    def get_upload_uri(self, task_metadata: Dict, object_key: str):
        dispatch_id = task_metadata["dispatch_id"]
        node_id = task_metadata["node_id"]

        filename = f"asset_{dispatch_id}-{node_id}_{object_key}.pkl"
        return os.path.join("file://", self.cache_dir, filename)