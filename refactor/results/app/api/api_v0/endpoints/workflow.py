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

import io
import logging
import logging.config
import os
import random
import sqlite3
import string
import time
from os import path
from tempfile import TemporaryFile
from typing import Any, BinaryIO, Optional, Tuple, Union

import cloudpickle as pickle
import requests
from app.schemas.common import HTTPExceptionSchema
from app.schemas.workflow import InsertResultResponse, Node, Result, UpdateResultResponse
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from refactor.results.app.core.config import settings
from refactor.results.app.core.get_svc_uri import DataURI

logging.config.fileConfig("logging.conf", disable_existing_loggers=False)

logger = logging.getLogger(__name__)

router = APIRouter()


# @router.middleware("http")
# TODO: figure out why the middleware doesn't work
async def log_requests(request: Request, call_next):
    idem = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    logger.info(f"rid={idem} start request path={request.url.path}")
    start_time = time.time()

    response = await call_next(request)

    process_time = (time.time() - start_time) * 1000
    formatted_process_time = "{0:.2f}".format(process_time)
    logger.info(
        f"rid={idem} completed_in={formatted_process_time}ms status_code={response.status_code}"
    )

    return response


def _get_result_file(dispatch_id: str) -> bytes:
    filename = _get_result_from_db(dispatch_id, "filename")
    path = _get_result_from_db(dispatch_id, "path")
    if not dispatch_id or not filename or not path:
        raise HTTPException(status_code=404, detail="Result was not found")
    r = requests.get(
        DataURI().get_route("/fs/download"), params={"file_location": filename}, stream=True
    )
    return r.content


def _upload_file(result_pkl_file: BinaryIO):
    results_object = {}
    dispatch_id = ""
    length = result_pkl_file.seek(0, 2)
    result_pkl_file.seek(0)
    try:
        results_object = pickle.load(result_pkl_file)
        dispatch_id = results_object.dispatch_id
        assert length > 0
    except:
        raise HTTPException(status_code=422, detail="Error in upload body.")
    result_pkl_file.seek(0)
    r = requests.post(
        DataURI().get_route("/fs/upload"),
        files=[("file", ("result.pkl", result_pkl_file, "application/octet-stream"))],
        params={"overwrite": True},
    )
    response = r.json()
    _handle_error_response(r.status_code, response)
    filename = response.get("filename")
    path = response.get("path")
    error_detail = "Error in response from data service. " + str(response)
    if filename and path:
        if _add_record_to_db(dispatch_id, filename, path):
            return {"dispatch_id": dispatch_id}
        else:
            error_detail = "Error adding record to database."
    raise HTTPException(status_code=500, detail="Error adding record to database.")


def _handle_error_response(status_code: int, response: dict):
    if status_code >= 400:
        raise HTTPException(status_code=status_code, detail=response["detail"])


def _db(sql: str, key: str = None) -> Optional[Tuple[Union[bool, str]]]:
    con = sqlite3.connect(settings.RESULTS_DB)
    cur = con.cursor()
    logger.info("Executing SQL command.")
    logger.info(sql)
    value = (False,)
    if key:
        logger.info("Searching for key " + key)
        cur.execute(sql, (key,))
        value = cur.fetchone()
    else:
        cur.execute(sql)
        value = (True,)
    con.commit()
    con.close()
    return value


def _get_result_from_db(dispatch_id: str, field: str) -> Optional[str]:
    sql = f"SELECT {field} FROM results WHERE dispatch_id=?"
    value = _db(sql, key=dispatch_id)
    if value:
        (value,) = value
    return value


def _add_record_to_db(dispatch_id: str, filename: str, path: str) -> None:
    sql = ""
    if _get_result_from_db(dispatch_id, "filename"):
        sql = (
            "UPDATE results "
            f"SET filename = '{filename}', path = '{path}' "
            f"WHERE dispatch_id = '{dispatch_id}' "
            # f"ORDER BY dispatch_id "
            # "LIMIT 1"
        )
    else:
        sql = f"INSERT INTO results VALUES('{dispatch_id}','{filename}','{path}')"
    insert = _db(sql)
    if insert:
        (insert,) = insert
    return insert


@router.get(
    "/results/{dispatch_id}",
    status_code=200,
    response_class=FileResponse,
    responses={
        404: {"model": HTTPExceptionSchema, "description": "Result was not found"},
        200: {
            "content": {"application/octet-stream": {}},
            "description": "Return binary content of file.",
        },
    },
)
def get_result(
    *,
    dispatch_id: str,
) -> Any:
    """
    Get a result object as pickle file
    """
    result: bytes = _get_result_file(dispatch_id)
    return StreamingResponse(io.BytesIO(result), media_type="application/octet-stream")


@router.post("/results", status_code=200, response_model=InsertResultResponse)
def insert_result(
    *,
    result_pkl_file: UploadFile,
) -> Any:
    """
    Submit pickled result file
    """
    return _upload_file(result_pkl_file.file)


@router.put(
    "/results/{dispatch_id}",
    status_code=200,
    responses={
        404: {"model": HTTPExceptionSchema, "description": "Result was not found"},
        200: {
            "model": UpdateResultResponse,
            "description": "Return message indicating success of updating task",
        },
    },
)
def update_result(*, dispatch_id: str, task: bytes = File(...)) -> Any:
    """
    Update a result object's task
    """
    result = _get_result_file(dispatch_id)
    results_object = pickle.loads(result)
    task = pickle.loads(task)
    results_object._update_node(**task)

    pickled_result = io.BytesIO(pickle.dumps(results_object))

    if _upload_file(pickled_result):
        return {"response": "Task updated successfully"}
