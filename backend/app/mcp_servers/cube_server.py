"""MCP server for the Cube Core semantic layer: governed measures and dimensions generated from the
approved semantic context. Cubes are filtered to the tables the calling user may read.

Run:  python -m app.mcp_servers.cube_server --user alice@example.com   (stdio)
"""
import argparse
import json
import time

import httpx
import jwt
from mcp.server.fastmcp import FastMCP
from sqlalchemy import select

from ..config import get_settings
from ..context.cube_model import cube_name
from ..context.service import ContextError, get_version
from ..db import Connection, SessionLocal, User
from ..observability.telemetry import record
from ..security.rbac import permitted


def _allowed_cubes(user_email: str) -> dict[str, str]:
    """cube name -> connection name, for approved tables the user may read."""
    out = {}
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == user_email))
        if not user:
            raise PermissionError(f"Unknown user {user_email}")
        for name, access in permitted(db, user).items():
            conn = db.scalar(select(Connection).where(Connection.name == name))
            try:
                v = get_version(db, conn, "approved")
            except ContextError:
                continue
            for t in v.metadata_layer["tables"]:
                if access.allows_table(t):
                    out[cube_name(t)] = name
    return out


def build_server(user_email: str) -> FastMCP:
    s = get_settings()
    if not s.cube_api_url:
        raise RuntimeError("CUBE_API_URL is not configured.")
    allowed = _allowed_cubes(user_email)

    def token() -> str:
        return jwt.encode({"sub": user_email, "exp": int(time.time()) + 300}, s.cube_api_secret, algorithm="HS256")

    def call(path: str, payload: dict | None = None) -> dict:
        with httpx.Client(base_url=s.cube_api_url.rstrip("/"), timeout=s.query_timeout_seconds + 10,
                          headers={"Authorization": token()}) as c:
            for _ in range(10):   # Cube answers "Continue wait" while a query is still running
                r = c.get(path) if payload is None else c.post(path, json={"query": payload})
                r.raise_for_status()
                body = r.json()
                if body.get("error") != "Continue wait":
                    return body
                time.sleep(1)
        return {"error": "Cube query timed out"}

    mcp = FastMCP("datafusion-cube", instructions="Governed metrics from the Cube semantic layer. "
                                                  "Call cube_meta first, then cube_query.")

    @mcp.tool()
    def cube_meta() -> str:
        """List the cubes, measures and dimensions (with approved descriptions) you may query."""
        record("tool", name="cube_meta", user=user_email)
        meta = call("/meta")
        cubes = [{"name": c["name"], "description": c.get("description", ""), "connection": allowed[c["name"]],
                  "measures": [{"name": m["name"], "description": m.get("description", "")} for m in c["measures"]],
                  "dimensions": [{"name": d["name"], "type": d["type"], "description": d.get("description", "")}
                                 for d in c["dimensions"]]}
                 for c in meta.get("cubes", []) if c["name"] in allowed]
        return json.dumps({"source": "Cube semantic layer", "cubes": cubes})

    @mcp.tool()
    def cube_query(measures: list[str], dimensions: list[str] | None = None, filters: list[dict] | None = None,
                   time_dimensions: list[dict] | None = None, limit: int = 100) -> str:
        """Run a Cube query. Members are 'cube.member', e.g. measures=['orders.count'],
        dimensions=['customers.country']. Filters use Cube's format: {member, operator, values}."""
        members = list(measures) + list(dimensions or []) + [f["member"] for f in (filters or []) if "member" in f] \
            + [t["dimension"] for t in (time_dimensions or []) if "dimension" in t]
        blocked = sorted({m.split(".")[0] for m in members if m.split(".")[0] not in allowed})
        if blocked:
            record("tool", name="cube_query", user=user_email, status="blocked", detail={"cubes": blocked})
            return json.dumps({"error": f"No read access to cubes: {', '.join(blocked)}"})
        q = {"measures": measures, "dimensions": dimensions or [], "filters": filters or [],
             "timeDimensions": time_dimensions or [], "limit": max(1, min(limit, s.query_row_limit))}
        started = time.monotonic()
        try:
            body = call("/load", q)
        except httpx.HTTPError as exc:
            record("tool", name="cube_query", user=user_email, status="error", detail={"error": str(exc)[:200]})
            return json.dumps({"error": f"Cube query failed: {exc}"})
        rows = body.get("data", [])
        conns = sorted({allowed[m.split(".")[0]] for m in members})
        record("tool", name="cube_query", user=user_email, connection=",".join(conns),
               latency_ms=int((time.monotonic() - started) * 1000), detail={"rows": len(rows)})
        return json.dumps({"source": f"Cube semantic layer over {', '.join(conns)}", "query": q,
                           "rows": rows, "row_count": len(rows)}, default=str)

    return mcp


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--user", required=True)
    a = p.parse_args()
    build_server(a.user).run(transport="stdio")


if __name__ == "__main__":
    main()
