"""Context research agent (Flow A, step 8) as a LangGraph graph.

load_inputs -> web_research (only when ON) -> describe_tables -> relationships -> knowledge -> save_draft

Inputs are the standard metadata, the steward's notes from the table / column page and the uploaded
documents. The output is a new *draft* version with a metadata layer and a knowledge base layer.
"""
import json
import re
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select

from ..db import CatalogTable, ContextVersion, Document, SessionLocal, StewardNote
from ..jsonutil import extract_json
from ..llm import chat_model
from ..observability.telemetry import record
from .web_search import search_terms, terms_from_tables

BATCH = 6
PII_HINTS = re.compile(r"(e-?mail|phone|mobile|ssn|social|passport|birth|dob|address|postcode|zip|iban|card)", re.I)

SYSTEM = """You are DataFusion's context research agent. You write clear, accurate business metadata for a
database so that a question-answering agent can use it. Use only the evidence provided: schema, statistics,
steward notes, documents and web findings. Never invent columns. If meaning is unclear, say so briefly.
Steward notes are authoritative. Reply with JSON only."""


class ResearchState(TypedDict, total=False):
    connection_id: int
    connection_name: str
    db_type: str
    user: str
    web_search: bool
    tables: list[dict]
    notes: list[dict]
    documents: list[dict]
    web_findings: list[dict]
    metadata_layer: dict
    knowledge_layer: dict
    log: list[str]
    version: int


def _table_payload(t: CatalogTable) -> dict:
    return {"name": t.qualified, "description": t.description, "row_count": t.row_count,
            "columns": [{k: c.get(k) for k in ("name", "type", "nullable", "comment", "pk", "profile")}
                        for c in t.columns],
            "foreign_keys": t.foreign_keys}


def load_inputs(state: ResearchState) -> ResearchState:
    with SessionLocal() as db:
        tables = db.scalars(select(CatalogTable).where(CatalogTable.connection_id == state["connection_id"])
                            .order_by(CatalogTable.schema_name, CatalogTable.table_name)).all()
        notes = db.scalars(select(StewardNote).where(StewardNote.connection_id == state["connection_id"])).all()
        docs = db.scalars(select(Document).where(Document.connection_id == state["connection_id"])).all()
    if not tables:
        raise ValueError("No standard metadata yet. Run the metadata catalog pipeline first.")
    return {"tables": [_table_payload(t) for t in tables],
            "notes": [{"table": n.table_name, "column": n.column_name, "description": n.description,
                       "business_meaning": n.business_meaning} for n in notes],
            "documents": [{"filename": d.filename, "table": d.table_name, "text": d.text} for d in docs],
            "log": [f"loaded {len(tables)} tables, {len(notes)} steward notes, {len(docs)} documents"]}


def web_research(state: ResearchState) -> ResearchState:
    if not state.get("web_search"):
        return {"web_findings": [], "log": state["log"] + ["web search OFF"]}
    terms = terms_from_tables(state["tables"])
    findings = search_terms(terms)
    return {"web_findings": findings, "log": state["log"] + [f"web search ON: {len(terms)} terms, {len(findings)} findings"]}


def _docs_excerpt(state: ResearchState, budget: int = 12_000) -> str:
    parts, used = [], 0
    for d in state.get("documents", []):
        chunk = d["text"][: max(0, budget - used)]
        if not chunk:
            break
        parts.append(f"--- {d['filename']} ---\n{chunk}")
        used += len(chunk)
    return "\n".join(parts)


def describe_tables(state: ResearchState) -> ResearchState:
    model = chat_model("research")
    notes_by_table: dict[str, list] = {}
    for n in state.get("notes", []):
        notes_by_table.setdefault(n["table"], []).append(n)
    docs = _docs_excerpt(state)
    web = json.dumps(state.get("web_findings", [])[:20])
    described: dict = {}
    tables = state["tables"]
    for i in range(0, len(tables), BATCH):
        batch = tables[i:i + BATCH]
        batch_notes = {t["name"]: notes_by_table.get(t["name"], []) for t in batch}
        prompt = (
            "Write metadata for these tables.\n"
            f"TABLES_JSON: {json.dumps(batch, default=str)} END_TABLES\n"
            f"STEWARD_NOTES: {json.dumps(batch_notes)}\n"
            f"DOCUMENTS:\n{docs or '(none)'}\n"
            f"WEB_FINDINGS: {web}\n"
            'Return {"tables": {"<table name>": {"description": str, "business_meaning": str, '
            '"columns": {"<column>": {"description": str, "business_meaning": str}}}}}')
        reply = model.invoke([SystemMessage(SYSTEM), HumanMessage(prompt)])
        record("llm", name="research.describe", connection=state["connection_name"], user=state.get("user", ""),
               usage=getattr(reply, "usage_metadata", None))
        described.update((extract_json(reply.content) or {}).get("tables", {}))

    layer_tables = {}
    for t in tables:
        d = described.get(t["name"]) or described.get(t["name"].split(".")[-1]) or {}
        dcols = d.get("columns", {})
        cols = {}
        for c in t["columns"]:
            dc = dcols.get(c["name"], {})
            cols[c["name"]] = {"type": c["type"], "nullable": c["nullable"], "primary_key": bool(c.get("pk")),
                               "description": dc.get("description") or c.get("comment") or "",
                               "business_meaning": dc.get("business_meaning", ""),
                               "pii": bool(PII_HINTS.search(c["name"])), "profile": c.get("profile", {})}
        entry = {"description": d.get("description") or t["description"] or "",
                 "business_meaning": d.get("business_meaning", ""), "row_count": t["row_count"],
                 "primary_key": [c["name"] for c in t["columns"] if c.get("pk")], "columns": cols,
                 "sources": ["standard metadata"]}
        # steward notes win over generated text
        for n in notes_by_table.get(t["name"], []):
            target = cols.get(n["column"]) if n["column"] else entry
            if target is None:
                continue
            if n["description"]:
                target["description"] = n["description"]
            if n["business_meaning"]:
                target["business_meaning"] = n["business_meaning"]
            if "steward notes" not in entry["sources"]:
                entry["sources"].append("steward notes")
        if state.get("documents"):
            entry["sources"].append("documents")
        layer_tables[t["name"]] = entry
    return {"metadata_layer": {"connection": state["connection_name"], "db_type": state["db_type"],
                               "tables": layer_tables, "relationships": []},
            "log": state["log"] + [f"described {len(layer_tables)} tables"]}


def relationships(state: ResearchState) -> ResearchState:
    """Declared foreign keys plus name-based candidates (customer_id -> customers.id), labelled by source."""
    rels, names = [], {t["name"].split(".")[-1].lower(): t["name"] for t in state["tables"]}
    for t in state["tables"]:
        declared = set()
        for fk in t["foreign_keys"]:
            rels.append({"from": f"{t['name']}.{','.join(fk['columns'])}",
                         "to": f"{fk['ref_table']}.{','.join(fk['ref_columns'])}", "type": "many_to_one",
                         "source": "foreign key"})
            declared.update(fk["columns"])
        for c in t["columns"]:
            m = re.match(r"(.+?)_id$", c["name"].lower())
            if not m or c["name"] in declared:
                continue
            base = m.group(1)
            target = names.get(base) or names.get(base + "s") or names.get(base + "es")
            if target and target != t["name"]:
                rels.append({"from": f"{t['name']}.{c['name']}", "to": f"{target}.id", "type": "many_to_one",
                             "source": "inferred from column name"})
    layer = dict(state["metadata_layer"])
    layer["relationships"] = rels
    return {"metadata_layer": layer, "log": state["log"] + [f"found {len(rels)} relationships"]}


def knowledge(state: ResearchState) -> ResearchState:
    model = chat_model("research")
    compact = [{"name": n, "description": t["description"],
                "columns": [{"name": c, "description": v["description"], "nullable": v["nullable"],
                             "pk": v["primary_key"]} for c, v in t["columns"].items()]}
               for n, t in state["metadata_layer"]["tables"].items()]
    prompt = ("KNOWLEDGE_REQUEST: build the knowledge base layer: business rules, glossary and notes.\n"
              f"TABLES_JSON: {json.dumps(compact)} END_TABLES\n"
              f"RELATIONSHIPS: {json.dumps(state['metadata_layer']['relationships'])}\n"
              f"DOCUMENTS:\n{_docs_excerpt(state, 8_000) or '(none)'}\n"
              'Return {"business_rules": [{"rule": str, "tables": [str], "source": str}], '
              '"glossary": [{"term": str, "definition": str, "related": [str]}], "notes": str}')
    reply = model.invoke([SystemMessage(SYSTEM), HumanMessage(prompt)])
    record("llm", name="research.knowledge", connection=state["connection_name"], user=state.get("user", ""),
           usage=getattr(reply, "usage_metadata", None))
    k = extract_json(reply.content)
    layer = {"business_rules": k.get("business_rules", []), "glossary": k.get("glossary", []),
             "relationships": state["metadata_layer"]["relationships"], "notes": k.get("notes", ""),
             "web_sources": [{"term": f["term"], "url": f["url"]} for f in state.get("web_findings", [])]}
    return {"knowledge_layer": layer,
            "log": state["log"] + [f"{len(layer['glossary'])} glossary terms, {len(layer['business_rules'])} rules"]}


def save_draft(state: ResearchState) -> ResearchState:
    with SessionLocal() as db:
        last = db.scalar(select(func.max(ContextVersion.version))
                         .where(ContextVersion.connection_id == state["connection_id"])) or 0
        v = ContextVersion(connection_id=state["connection_id"], version=last + 1, status="draft",
                           metadata_layer=state["metadata_layer"], knowledge_layer=state["knowledge_layer"],
                           research_log=state["log"], created_by=state.get("user", ""))
        db.add(v)
        db.commit()
        return {"version": v.version}


def build_research_graph():
    g = StateGraph(ResearchState)
    for name, fn in [("load_inputs", load_inputs), ("web_research", web_research), ("describe_tables", describe_tables),
                     ("relationships", relationships), ("knowledge", knowledge), ("save_draft", save_draft)]:
        g.add_node(name, fn)
    g.add_edge(START, "load_inputs")
    g.add_edge("load_inputs", "web_research")
    g.add_edge("web_research", "describe_tables")
    g.add_edge("describe_tables", "relationships")
    g.add_edge("relationships", "knowledge")
    g.add_edge("knowledge", "save_draft")
    g.add_edge("save_draft", END)
    return g.compile()


def run_research(connection_id: int, connection_name: str, db_type: str, user: str, web_search: bool) -> dict:
    result = build_research_graph().invoke({"connection_id": connection_id, "connection_name": connection_name,
                                            "db_type": db_type, "user": user, "web_search": web_search})
    return {"version": result["version"], "status": "draft", "log": result["log"],
            "tables": len(result["metadata_layer"]["tables"]),
            "glossary_terms": len(result["knowledge_layer"]["glossary"])}
