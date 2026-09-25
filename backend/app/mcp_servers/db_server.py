"""Auto-generated MCP server: one per connected database.

Built at start-up from the connection's *approved* semantic context and the calling user's permissions.
Uses the stored (encrypted) credentials, runs read-only queries only, and returns results with the
source and executed SQL so every fact can be cited.

Run:  python -m app.mcp_servers.db_server --connection sales --user alice@example.com   (stdio)
"""
import argparse
import json
import re
import time
from datetime import date, datetime
from decimal import Decimal

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select, text

from ..config import get_settings
from ..connectors.registry import spec_for
from ..connectors.service import get_connection, readonly_engine_for
from ..context.service import ContextError, context_payload, get_version
from ..db import SessionLocal, User
from ..observability.telemetry import record
from ..security.rbac import permitted
from ..agent.guardrails import GuardrailViolation, validate_select

MAX_TABLE_TOOLS = 25


def _json(value) -> str:
    def default(o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, (bytes, bytearray)):
            return f"<{len(o)} bytes>"
        return str(o)
    return json.dumps(value, default=default)


def build_server(connection_name: str, user_email: str) -> FastMCP:
    s = get_settings()
    with SessionLocal() as db:
        conn = get_connection(db, connection_name)
        user = db.scalar(select(User).where(User.email == user_email))
        if user is None:
            raise PermissionError(f"Unknown user {user_email}")
        access = permitted(db, user).get(connection_name)
        if access is None:
            raise PermissionError(f"{user_email} has no read access to '{connection_name}'.")
        try:
            version = get_version(db, conn, "approved")
        except ContextError as exc:
            raise RuntimeError(f"'{connection_name}' has no approved semantic context yet.") from exc
        allowed = None if access.tables is None else {
            t for t in version.metadata_layer["tables"] if access.allows_table(t)}
        ctx = context_payload(conn, version, sorted(allowed) if allowed is not None else None, access.denied_columns)
        db_type = conn.db_type
        engine = readonly_engine_for(conn)
    tables = ctx["metadata_layer"]["tables"]
    dialect = spec_for(db_type).sqlglot_dialect
    known = set(tables)
    source = f"{connection_name} (read-only, semantic context v{ctx['version']})"

    mcp = FastMCP(f"datafusion-{connection_name}",
                  instructions=f"Read-only access to the '{connection_name}' database ({db_type}). "
                               f"Descriptions come from approved semantic context version {ctx['version']}. "
                               "Call list_tables / describe_table before writing SQL.")

    def run(sql: str, limit: int) -> str:
        started = time.monotonic()
        limit = max(1, min(int(limit or s.query_row_limit), s.query_row_limit))
        try:
            final_sql = validate_select(sql, dialect, allowed, access.denied_columns, known, limit)
            with engine.connect() as c:
                res = c.execute(text(final_sql))
                cols = list(res.keys())
                rows = [dict(zip(cols, r)) for r in res.fetchmany(limit)]
            record("tool", name="run_readonly_query", user=user_email, connection=connection_name,
                   latency_ms=int((time.monotonic() - started) * 1000), detail={"rows": len(rows)})
            return _json({"source": source, "connection": connection_name, "context_version": ctx["version"],
                          "sql": final_sql, "columns": cols, "rows": rows, "row_count": len(rows),
                          "truncated": len(rows) >= limit})
        except GuardrailViolation as exc:
            record("tool", name="run_readonly_query", user=user_email, connection=connection_name, status="blocked",
                   detail={"reason": str(exc)})
            return _json({"error": f"Blocked by guardrails: {exc}", "sql": sql})
        except Exception as exc:  # noqa: BLE001
            record("tool", name="run_readonly_query", user=user_email, connection=connection_name, status="error",
                   detail={"error": str(exc)[:300]})
            return _json({"error": f"Query failed: {str(exc).splitlines()[0][:300]}", "sql": sql})

    @mcp.tool()
    def list_tables() -> str:
        """List the tables you may query, with their approved business descriptions."""
        record("tool", name="list_tables", user=user_email, connection=connection_name)
        return _json({"source": source, "tables": [
            {"table": n, "description": t["description"], "business_meaning": t.get("business_meaning", ""),
             "row_count": t.get("row_count")} for n, t in tables.items()]})

    @mcp.tool()
    def describe_table(table: str) -> str:
        """Columns (type, description, business meaning, PII flag), primary key and relationships for a table."""
        record("tool", name="describe_table", user=user_email, connection=connection_name)
        name = table if table in tables else next((n for n in tables if n.split(".")[-1] == table), None)
        if not name:
            return _json({"error": f"Unknown or restricted table '{table}'.", "available": sorted(tables)})
        rels = [r for r in ctx["metadata_layer"]["relationships"] if name in (r["from"].rsplit(".", 1)[0],
                                                                                 r["to"].rsplit(".", 1)[0])]
        return _json({"source": source, "table": name, **tables[name], "relationships": rels})

    @mcp.tool()
    def search_context(term: str) -> str:
        """Search the approved glossary, business rules and descriptions for a business term."""
        record("tool", name="search_context", user=user_email, connection=connection_name)
        needle = term.lower()
        k = ctx["knowledge_layer"]
        hits = {"glossary": [g for g in k.get("glossary", []) if needle in json.dumps(g).lower()],
                "business_rules": [r for r in k.get("business_rules", []) if needle in json.dumps(r).lower()],
                "tables": [n for n, t in tables.items() if needle in (n + json.dumps(t)).lower()]}
        return _json({"source": source, "term": term, **hits})

    @mcp.tool()
    def run_readonly_query(sql: str, limit: int = 100) -> str:
        """Run ONE read-only SELECT. Guardrails enforce read access, restricted columns and a row limit."""
        return run(sql, limit)

    # Tools generated from the semantic context: one structured query tool per table
    for tname in list(tables)[:MAX_TABLE_TOOLS]:
        t = tables[tname]
        tool_name = "query_" + re.sub(r"[^a-z0-9_]", "_", tname.lower())

        def make(tn: str):
            def query_table(columns: list[str] | None = None, where: str = "", order_by: str = "",
                            limit: int = 50) -> str:
                cols = ", ".join(f'"{c}"' for c in (columns or tables[tn]["columns"]))
                schema, _, name = tn.rpartition(".")
                ref = f'"{schema}"."{name}"' if schema else f'"{name}"'
                sql = f"SELECT {cols} FROM {ref}"
                if where:
                    sql += f" WHERE {where}"
                if order_by:
                    sql += f" ORDER BY {order_by}"
                return run(sql, limit)
            return query_table

        col_list = ", ".join(list(t["columns"])[:30])
        mcp.add_tool(make(tname), name=tool_name,
                     description=f"Query {tname}: {t['description'][:200]} Columns: {col_list}. "
                                 "Optional where / order_by are SQL fragments; guardrails still apply.")
    return mcp


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--connection", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http"])
    a = p.parse_args()
    build_server(a.connection, a.user).run(transport=a.transport)


if __name__ == "__main__":
    main()
