"""Keep long requests alive behind proxies that drop idle connections.

The work runs in the background while single spaces are streamed every few seconds; the JSON result
follows. Because the HTTP status is sent before the work finishes, failures are reported in the body as
{"detail": ..., "datafusion_error": true}; Apache Hop's pipelines check for that marker as well as the
status code.
"""
import asyncio
import json
from typing import Any, Awaitable, Callable

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import get_settings


async def respond(work: Callable[[], Awaitable[Any]]):
    s = get_settings()
    if not s.keepalive_enabled:
        return await work()

    async def body():
        task = asyncio.create_task(work())
        while True:
            done, _ = await asyncio.wait({task}, timeout=s.keepalive_interval_seconds)
            if done:
                break
            yield b" "
        try:
            result = task.result()
            if isinstance(result, JSONResponse):
                yield result.body
            else:
                yield json.dumps(jsonable_encoder(result)).encode()
        except HTTPException as exc:
            yield json.dumps({"detail": exc.detail, "status_code": exc.status_code, "datafusion_error": True}).encode()
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"detail": f"Internal error: {str(exc)[:300]}", "datafusion_error": True}).encode()

    return StreamingResponse(body(), media_type="application/json")
