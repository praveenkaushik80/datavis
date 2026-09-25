"""Telemetry for the Observability UI: user queries, agent execution, token and LLM usage, databases and
MCP tools used, API calls, errors and execution time. Never records credentials or row data."""
import logging
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import func, select

from ..db import SessionLocal, Telemetry

log = logging.getLogger("datafusion.telemetry")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def record(kind: str, *, name: str = "", user: str = "", connection: str = "", status: str = "ok",
           latency_ms: int = 0, usage: dict | None = None, trace_id: str = "", detail: dict | None = None) -> None:
    usage = usage or {}
    try:
        with SessionLocal() as db:
            db.add(Telemetry(kind=kind, name=name, user=user, connection=connection, status=status,
                             latency_ms=latency_ms, tokens_in=int(usage.get("input_tokens", 0) or 0),
                             tokens_out=int(usage.get("output_tokens", 0) or 0), trace_id=trace_id,
                             detail=detail or {}))
            db.commit()
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a request
        log.warning("telemetry write failed: %s", exc)


@contextmanager
def timed(kind: str, **kw):
    start, status = time.monotonic(), "ok"
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        record(kind, status=status, latency_ms=int((time.monotonic() - start) * 1000), **kw)


def summary(hours: int = 24) -> dict:
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with SessionLocal() as db:
        rows = db.execute(select(Telemetry.kind, Telemetry.status, func.count(), func.sum(Telemetry.tokens_in),
                                 func.sum(Telemetry.tokens_out), func.avg(Telemetry.latency_ms))
                          .where(Telemetry.ts >= since).group_by(Telemetry.kind, Telemetry.status)).all()
        tools = db.execute(select(Telemetry.name, func.count()).where(Telemetry.kind == "tool", Telemetry.ts >= since)
                           .group_by(Telemetry.name)).all()
        dbs = db.execute(select(Telemetry.connection, func.count()).where(Telemetry.kind == "tool",
                                                                          Telemetry.ts >= since)
                         .group_by(Telemetry.connection)).all()
        questions = db.scalar(select(func.count()).where(Telemetry.kind == "agent", Telemetry.ts >= since))
    return {"window_hours": hours, "user_questions": questions or 0,
            "by_kind": [{"kind": k, "status": s, "count": n, "tokens_in": int(ti or 0), "tokens_out": int(to or 0),
                         "avg_latency_ms": int(lat or 0)} for k, s, n, ti, to, lat in rows],
            "tools_used": {n: c for n, c in tools}, "databases_accessed": {n: c for n, c in dbs if n}}
