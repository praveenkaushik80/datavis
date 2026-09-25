"""Step 3 of the connection wizard: verify access and list schemas and tables. Nothing is saved."""
import time

from sqlalchemy import create_engine, inspect, text

from ..security.crypto import scrub
from .registry import build_url, spec_for

SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast", "sys", "mysql", "performance_schema",
                  "INFORMATION_SCHEMA", "SYS", "SYSTEM", "guest", "db_owner"}


def friendly_error(exc: Exception, password: str) -> str:
    msg = scrub(str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__, password)
    lowered = msg.lower()
    if "password authentication failed" in lowered or "access denied" in lowered or "login failed" in lowered:
        return "Login failed: check the username and password."
    if "could not translate host name" in lowered or "name or service not known" in lowered:
        return "Host not found: check the host name."
    if "connection refused" in lowered or "timed out" in lowered or "timeout" in lowered:
        return "Could not reach the database: check host, port and firewall rules."
    if "does not exist" in lowered or "unknown database" in lowered:
        return "Database not found: check the database name."
    if "no module named" in lowered:
        return f"Driver not installed for this database type ({msg})."
    return f"Connection failed: {msg}"


def verify_connection(db_type: str, host: str, port: int | None, database: str, username: str, password: str,
                      options: dict | None = None) -> dict:
    spec = spec_for(db_type)
    started = time.monotonic()
    try:
        url = build_url(db_type, host, port, database, username, password, options)
        eng = create_engine(url, pool_pre_ping=True)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1" if db_type != "oracle" else "SELECT 1 FROM dual"))
        insp = inspect(eng)
        schemas = [s for s in insp.get_schema_names() if s not in SYSTEM_SCHEMAS] if db_type != "sqlite" else ["main"]
        wanted = (options or {}).get("schemas") or schemas
        tables = {}
        for s in wanted:
            names = insp.get_table_names(schema=None if db_type == "sqlite" else s)
            tables[s] = sorted(names)
        eng.dispose()
        return {"ok": True, "db_type": spec.label, "schemas": list(tables), "tables": tables,
                "table_count": sum(len(v) for v in tables.values()),
                "latency_ms": int((time.monotonic() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001 - every failure becomes a clear message
        return {"ok": False, "error": friendly_error(exc, password),
                "latency_ms": int((time.monotonic() - started) * 1000)}
