import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api.routes import internal, router
from .db import init_db
from .observability.telemetry import record

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")



@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="DataFusion API", description="Semantic context (Flow A) and governed agent (Flow B).",
              lifespan=lifespan)
app.include_router(router)
app.include_router(internal)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    record("error", name=request.url.path, status="error", detail={"error": str(exc)[:500]})
    logging.getLogger("datafusion").exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal error; see server logs."})


@app.get("/health")
def health():
    return {"status": "ok"}
