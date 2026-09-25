"""OpenMetadata integration: catalog of record for standard metadata and approved descriptions.

Uses the OpenMetadata REST API (v1). The service registration and ingestion pipeline mirror what the
OpenMetadata UI does; ingestion itself runs in OpenMetadata's ingestion container. Endpoints and
connection shapes are checked against the OpenMetadata 2.0.2 JSON schemas.
"""
import logging
import re
import time

import httpx

from ..config import get_settings
from ..connectors.registry import spec_for
from ..db import Connection
from ..security import crypto

log = logging.getLogger(__name__)


class OpenMetadataError(RuntimeError):
    pass


def enabled() -> bool:
    s = get_settings()
    return bool(s.om_url and s.om_token)


def _client() -> httpx.Client:
    s = get_settings()
    return httpx.Client(base_url=s.om_url.rstrip("/") + "/api/v1", timeout=60,
                        headers={"Authorization": f"Bearer {s.om_token}"})


def _raise(resp: httpx.Response, what: str) -> dict:
    if resp.status_code >= 400:
        raise OpenMetadataError(f"OpenMetadata {what} failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json() if resp.content else {}


def service_config(conn: Connection) -> dict:
    """Connection config per OpenMetadata service type."""
    spec = spec_for(conn.db_type)
    password = crypto.decrypt(conn.secret_enc)
    host_port = f"{conn.host}:{conn.port or spec.default_port}"
    o = conn.options or {}
    if conn.db_type == "postgres":
        return {"type": "Postgres", "scheme": "postgresql+psycopg2", "username": conn.username,
                "authType": {"password": password}, "hostPort": host_port, "database": conn.database}
    if conn.db_type == "mysql":
        return {"type": "Mysql", "scheme": "mysql+pymysql", "username": conn.username,
                "authType": {"password": password}, "hostPort": host_port, "databaseName": conn.database}
    if conn.db_type == "mssql":
        return {"type": "Mssql", "scheme": "mssql+pytds", "username": conn.username, "password": password,
                "hostPort": host_port, "database": conn.database}
    if conn.db_type == "oracle":
        return {"type": "Oracle", "scheme": "oracle+cx_oracle", "username": conn.username, "password": password,
                "hostPort": host_port,
                "oracleConnectionType": {"oracleServiceName": o.get("service_name") or conn.database}}
    if conn.db_type == "snowflake":
        return {"type": "Snowflake", "username": conn.username, "password": password,
                "account": o.get("account") or conn.host, "warehouse": o.get("warehouse", ""),
                "database": conn.database, "role": o.get("role", "")}
    raise OpenMetadataError(f"{spec.label} is not supported by the OpenMetadata integration.")


def register_service(conn: Connection) -> str:
    spec = spec_for(conn.db_type)
    body = {"name": f"datafusion_{conn.name}", "serviceType": spec.om_service_type,
            "description": f"Registered by DataFusion (connection '{conn.name}').",
            "connection": {"config": service_config(conn)}}
    with _client() as c:
        svc = _raise(c.put("/services/databaseServices", json=body), "service registration")
    return svc["name"]


def run_metadata_ingestion(service_name: str) -> dict:
    """Create (or reuse), deploy and trigger the metadata ingestion pipeline for a service."""
    with _client() as c:
        svc = _raise(c.get(f"/services/databaseServices/name/{service_name}"), "service lookup")
        pipe_name = f"{service_name}_metadata"
        existing = c.get(f"/services/ingestionPipelines/name/{service_name}.{pipe_name}")
        if existing.status_code == 200:
            pipe = existing.json()
        else:
            pipe = _raise(c.post("/services/ingestionPipelines", json={
                "name": pipe_name, "displayName": f"DataFusion metadata ({service_name})",
                "pipelineType": "metadata",
                "service": {"id": svc["id"], "type": "databaseService"},
                "sourceConfig": {"config": {"type": "DatabaseMetadata", "includeTables": True,
                                            "includeViews": True, "markDeletedTables": True}},
                "airflowConfig": {}}), "ingestion pipeline creation")
        _raise(c.post(f"/services/ingestionPipelines/deploy/{pipe['id']}"), "pipeline deploy")
        _raise(c.post(f"/services/ingestionPipelines/trigger/{pipe['id']}"), "pipeline trigger")
    return {"pipeline": pipe.get("fullyQualifiedName", pipe_name), "status": "triggered"}


def list_tables(service_name: str) -> list[dict]:
    out, after = [], None
    with _client() as c:
        while True:
            params = {"service": service_name, "fields": "columns,tableConstraints,description", "limit": 100}
            if after:
                params["after"] = after
            page = _raise(c.get("/tables", params=params), "table listing")
            out.extend(page.get("data", []))
            after = (page.get("paging") or {}).get("after")
            if not after:
                return out


def wait_for_tables(service_name: str, timeout_s: int = 300, poll_s: int = 10) -> list[dict]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        tables = list_tables(service_name)
        if tables:
            return tables
        time.sleep(poll_s)
    return []


def push_descriptions(service_name: str, metadata_layer: dict) -> dict:
    """Write approved table and column descriptions back to OpenMetadata (JSON Patch)."""
    by_name = {}
    for t in list_tables(service_name):
        schema = (t.get("databaseSchema") or {}).get("name", "")
        by_name[f"{schema}.{t['name']}".lower()] = t
        by_name[t["name"].lower()] = t
    updated, missing = 0, []
    with _client() as c:
        for tname, tctx in (metadata_layer.get("tables") or {}).items():
            om = by_name.get(tname.lower()) or by_name.get(tname.split(".")[-1].lower())
            if not om:
                missing.append(tname)
                continue
            ops = []
            if tctx.get("description"):
                ops.append({"op": "add", "path": "/description", "value": tctx["description"]})
            col_index = {col["name"].lower(): i for i, col in enumerate(om.get("columns", []))}
            for cname, cctx in (tctx.get("columns") or {}).items():
                i = col_index.get(cname.lower())
                if i is not None and cctx.get("description"):
                    ops.append({"op": "add", "path": f"/columns/{i}/description", "value": cctx["description"]})
            if ops:
                _raise(c.patch(f"/tables/{om['id']}", json=ops,
                               headers={"Content-Type": "application/json-patch+json"}), "description update")
                updated += 1
    return {"tables_updated": updated, "not_found": missing}


def push_glossary(connection_name: str, knowledge_layer: dict) -> dict:
    terms = knowledge_layer.get("glossary") or []
    if not terms:
        return {"terms": 0}
    gname = f"datafusion_{connection_name}"
    with _client() as c:
        _raise(c.put("/glossaries", json={"name": gname, "displayName": f"DataFusion: {connection_name}",
                                          "description": "Approved business glossary from DataFusion."}), "glossary")
        n = 0
        for term in terms:
            name = re.sub(r"[^A-Za-z0-9_ -]", "", term.get("term", "")).strip()[:120]
            if not name:
                continue
            _raise(c.put("/glossaryTerms", json={"glossary": gname, "name": name, "displayName": term.get("term"),
                                                 "description": term.get("definition") or name}), "glossary term")
            n += 1
    return {"glossary": gname, "terms": n}
