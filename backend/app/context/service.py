"""Review, edit and approve (Flow A, steps 9-11) and the Context API payloads."""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import Connection, ContextVersion
from ..metadata import openmetadata as om
from . import cube_model

log = logging.getLogger(__name__)


class ContextError(ValueError):
    pass


def get_version(db: Session, conn: Connection, version: int | str) -> ContextVersion:
    q = select(ContextVersion).where(ContextVersion.connection_id == conn.id)
    if version == "latest":
        q = q.order_by(ContextVersion.version.desc())
    elif version == "approved":
        q = q.where(ContextVersion.status == "approved").order_by(ContextVersion.version.desc())
    else:
        q = q.where(ContextVersion.version == int(version))
    v = db.scalars(q.limit(1)).first()
    if not v:
        raise ContextError(f"No {'approved ' if version == 'approved' else ''}context version "
                           f"{'' if version in ('latest', 'approved') else version} for '{conn.name}'.".replace("  ", " "))
    return v


def version_summary(v: ContextVersion) -> dict:
    return {"version": v.version, "status": v.status, "created_by": v.created_by, "created_at": v.created_at,
            "decided_by": v.decided_by, "decided_at": v.decided_at, "decision_note": v.decision_note,
            "tables": len((v.metadata_layer or {}).get("tables", {})),
            "glossary_terms": len((v.knowledge_layer or {}).get("glossary", []))}


def apply_edits(db: Session, v: ContextVersion, edits: dict, user: str) -> ContextVersion:
    """Merge reviewer edits. Shape: {"tables": {t: {"description", "business_meaning", "columns": {c: {...}}}},
    "glossary": [...], "business_rules": [...]} ; only given keys change."""
    if v.status != "draft":
        raise ContextError(f"Version {v.version} is {v.status}; only drafts can be edited.")
    meta = json.loads(json.dumps(v.metadata_layer))
    know = json.loads(json.dumps(v.knowledge_layer))
    for tname, te in (edits.get("tables") or {}).items():
        if tname not in meta["tables"]:
            raise ContextError(f"Unknown table in edits: {tname}")
        t = meta["tables"][tname]
        for k in ("description", "business_meaning"):
            if k in te:
                t[k] = te[k]
        for cname, ce in (te.get("columns") or {}).items():
            if cname not in t["columns"]:
                raise ContextError(f"Unknown column in edits: {tname}.{cname}")
            for k in ("description", "business_meaning", "pii"):
                if k in ce:
                    t["columns"][cname][k] = ce[k]
        if "reviewer edits" not in t["sources"]:
            t["sources"].append("reviewer edits")
    for k in ("glossary", "business_rules", "notes"):
        if k in edits:
            know[k] = edits[k]
    v.metadata_layer, v.knowledge_layer = meta, know
    v.research_log = list(v.research_log or []) + [f"edited by {user}"]
    db.commit()
    return v


def review_document(conn: Connection, v: ContextVersion) -> dict:
    """The editable view of a version: descriptions, business meaning, PII flags, glossary, rules.
    Hop saves it to a local file; the steward edits it and posts it back to /edits/raw."""
    return {"connection": conn.name, "version": v.version, "status": v.status,
                "tables": {n: {"description": t["description"], "business_meaning": t["business_meaning"],
                               "columns": {c: {"description": cv["description"],
                                               "business_meaning": cv["business_meaning"], "pii": cv["pii"]}
                                           for c, cv in t["columns"].items()}}
                           for n, t in v.metadata_layer["tables"].items()},
                "glossary": v.knowledge_layer.get("glossary", []),
                "business_rules": v.knowledge_layer.get("business_rules", [])}


def export_for_review(conn: Connection, v: ContextVersion) -> Path | None:
    """Docker only: also write the review document to the shared folder, when one is configured."""
    if not get_settings().shared_dir:
        return None
    d = get_settings().review_dir
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{conn.name}-context-{v.version}.json"
    path.write_text(json.dumps(review_document(conn, v), indent=2, default=str))
    return path


def decide(db: Session, conn: Connection, v: ContextVersion, approve: bool, user: str, note: str = "") -> dict:
    if v.status != "draft":
        raise ContextError(f"Version {v.version} is already {v.status}.")
    now = datetime.now(timezone.utc)
    v.decided_by, v.decided_at, v.decision_note = user, now, note
    result: dict = {"version": v.version}
    if not approve:
        v.status = "rejected"
        db.commit()
        return result | {"status": "rejected"}
    for prev in db.scalars(select(ContextVersion).where(ContextVersion.connection_id == conn.id,
                                                        ContextVersion.status == "approved")):
        prev.status = "superseded"
    v.status = "approved"
    db.commit()
    result["status"] = "approved"
    # publish: Cube data model + OpenMetadata descriptions / glossary (best effort, reported back)
    try:
        result["cube_model"] = cube_model.publish(db, conn.name, v.version, v.metadata_layer)
    except Exception as exc:  # noqa: BLE001
        result["cube_model_error"] = str(exc)
    if om.enabled() and conn.om_service:
        try:
            result["openmetadata"] = om.push_descriptions(conn.om_service, v.metadata_layer)
            result["openmetadata_glossary"] = om.push_glossary(conn.name, v.knowledge_layer)
        except Exception as exc:  # noqa: BLE001
            result["openmetadata_error"] = str(exc)
    return result


def context_payload(conn: Connection, v: ContextVersion, tables: list[str] | None = None,
                    denied_columns: set[str] | None = None) -> dict:
    """Context API response, filtered to what the caller may see."""
    denied_columns = denied_columns or set()
    meta_tables = {}
    for n, t in v.metadata_layer.get("tables", {}).items():
        if tables is not None and n not in tables:
            continue
        cols = {c: cv for c, cv in t["columns"].items() if f"{n}.{c}" not in denied_columns}
        meta_tables[n] = {**t, "columns": cols}
    rels = [r for r in v.metadata_layer.get("relationships", [])
            if tables is None or (r["from"].rsplit(".", 1)[0] in meta_tables and r["to"].rsplit(".", 1)[0] in meta_tables)]
    return {"connection": conn.name, "db_type": conn.db_type, "version": v.version, "status": v.status,
            "approved_at": v.decided_at, "approved_by": v.decided_by,
            "metadata_layer": {"tables": meta_tables, "relationships": rels},
            "knowledge_layer": v.knowledge_layer}
