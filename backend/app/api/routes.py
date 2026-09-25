"""REST API used by Apache Hop workflows (and any future web UI)."""
import asyncio
import hmac
import json
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.graph import answer_question
from . import keepalive
from ..config import get_settings
from ..connectors.registry import CONNECTORS
from ..connectors.service import ConnectionError_, get_connection, public_view, register_connection
from ..connectors.verify import verify_connection
from ..context import cube_model
from ..context import service as ctx
from ..context.documents import DocumentError, parse_file, resolve_shared_path
from ..context.research import run_research
from ..db import SessionLocal, CatalogTable, Connection, ContextVersion, CubeModel, Document, Permission, StewardNote, Telemetry, User
from ..metadata import openmetadata as om
from ..metadata.extract import extract_catalog
from ..observability import telemetry
from ..security import crypto
from ..security.auth import current_user, ensure_user, get_db, request_otp, require_role, verify_otp
from ..security.rbac import permitted

router = APIRouter(prefix="/api")
steward = require_role("steward", "admin")
admin = require_role("admin")


def _conn(db: Session, name: str) -> Connection:
    try:
        return get_connection(db, name)
    except ConnectionError_ as exc:
        raise HTTPException(404, str(exc)) from exc


# ---------------------------------------------------------------- auth
class OtpRequest(BaseModel):
    email: str


class OtpVerify(BaseModel):
    email: str
    code: str


@router.post("/auth/otp/request")
def otp_request(body: OtpRequest, db: Session = Depends(get_db)):
    request_otp(db, body.email)
    return {"sent": True}


@router.post("/auth/otp/verify")
def otp_verify(body: OtpVerify, db: Session = Depends(get_db)):
    return {"access_token": verify_otp(db, body.email, body.code), "token_type": "bearer"}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"email": user.email, "role": user.role, "department": user.department}


# ---------------------------------------------------------------- Flow A, ribbon 1: database connection
class ConnectionIn(BaseModel):
    name: str
    db_type: str
    host: str = ""
    port: int | None = None
    database: str = ""
    username: str = ""
    password: str = ""
    options: dict = Field(default_factory=dict)
    run_catalog: bool = True


@router.get("/connectors")
def connectors():
    return [{"db_type": c.db_type, "label": c.label, "release": c.release, "default_port": c.default_port}
            for c in CONNECTORS.values() if c.release != "dev"]


@router.post("/connections/verify")
def verify(body: ConnectionIn, user: User = Depends(steward)):
    """Step 3: test access, list schemas and tables. Nothing is saved."""
    if body.db_type not in CONNECTORS:
        raise HTTPException(400, f"Unsupported database type '{body.db_type}'.")
    result = verify_connection(body.db_type, body.host, body.port, body.database, body.username, body.password,
                               body.options)
    telemetry.record("api", name="connections.verify", user=user.email, status="ok" if result["ok"] else "error")
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    return result


@router.post("/connections", status_code=201)
def create_connection(body: ConnectionIn, user: User = Depends(steward), db: Session = Depends(get_db)):
    """Steps 2-5: verify, store credentials encrypted, register in OpenMetadata, run the catalog pipeline."""
    try:
        conn, check = register_connection(db, name=body.name, db_type=body.db_type, host=body.host, port=body.port,
                                          database=body.database, username=body.username, password=body.password,
                                          options=body.options)
    except (ConnectionError_, crypto.CredentialError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    out = {"connection": public_view(conn), "verification": check}
    if body.run_catalog:
        out["catalog"] = _run_catalog(db, conn)
    telemetry.record("api", name="connections.create", user=user.email, connection=conn.name)
    return out


def _run_catalog(db: Session, conn: Connection) -> dict:
    result = {"standard_metadata": extract_catalog(db, conn)}
    if om.enabled() and CONNECTORS[conn.db_type].om_service_type:
        try:
            conn.om_service = om.register_service(conn)
            db.commit()
            result["openmetadata"] = {"service": conn.om_service}
            if get_settings().om_run_ingestion:
                result["openmetadata"].update(om.run_metadata_ingestion(conn.om_service))
        except om.OpenMetadataError as exc:
            result["openmetadata_error"] = str(exc)
    else:
        result["openmetadata"] = "not configured"
    return result


@router.get("/connections")
def list_connections(user: User = Depends(current_user), db: Session = Depends(get_db)):
    allowed = permitted(db, user)
    return [public_view(c) for c in db.scalars(select(Connection)) if c.name in allowed]


@router.post("/connections/{name}/catalog/run")
def run_catalog(name: str, user: User = Depends(steward), db: Session = Depends(get_db)):
    conn = _conn(db, name)
    return _run_catalog(db, conn)


# ---------------------------------------------------------------- Flow A, ribbon 2: enhancement
@router.get("/connections/{name}/tables")
def tables(name: str, user: User = Depends(steward), db: Session = Depends(get_db)):
    conn = _conn(db, name)
    return [{"table": t.qualified, "description": t.description, "row_count": t.row_count,
             "columns": len(t.columns)} for t in conn.tables]


@router.get("/connections/{name}/tables/{table}")
def table_detail(name: str, table: str, user: User = Depends(steward), db: Session = Depends(get_db)):
    conn = _conn(db, name)
    t = next((t for t in conn.tables if table in (t.qualified, t.table_name)), None)
    if not t:
        raise HTTPException(404, f"Unknown table '{table}'.")
    notes = db.scalars(select(StewardNote).where(StewardNote.connection_id == conn.id,
                                                 StewardNote.table_name == t.qualified)).all()
    return {"table": t.qualified, "description": t.description, "row_count": t.row_count, "columns": t.columns,
            "foreign_keys": t.foreign_keys,
            "notes": [{"column": n.column_name, "description": n.description,
                       "business_meaning": n.business_meaning, "author": n.author} for n in notes]}


class NoteIn(BaseModel):
    column: str | None = None
    description: str = ""
    business_meaning: str = ""


@router.post("/connections/{name}/tables/{table}/notes", status_code=201)
def add_note(name: str, table: str, body: NoteIn, user: User = Depends(steward), db: Session = Depends(get_db)):
    """Table / column page: edit the description, add business meaning and requirements."""
    conn = _conn(db, name)
    t = next((t for t in conn.tables if table in (t.qualified, t.table_name)), None)
    if not t:
        raise HTTPException(404, f"Unknown table '{table}'.")
    column = body.column or None
    if column and column not in {c["name"] for c in t.columns}:
        raise HTTPException(400, f"Unknown column '{column}' in {t.qualified}.")
    db.add(StewardNote(connection_id=conn.id, table_name=t.qualified, column_name=column,
                       description=body.description, business_meaning=body.business_meaning, author=user.email))
    db.commit()
    return {"saved": True, "table": t.qualified, "column": column}


def _store_document(db: Session, conn: Connection, path: Path, filename: str, table: str | None) -> dict:
    try:
        text = parse_file(path)
    except DocumentError as exc:
        raise HTTPException(400, str(exc)) from exc
    doc = Document(connection_id=conn.id, filename=filename, table_name=table, text=text)
    db.add(doc)
    db.commit()
    return {"document_id": doc.id, "filename": filename, "characters": len(text)}


@router.post("/connections/{name}/documents", status_code=201)
async def upload_document(name: str, file: UploadFile = File(...), table: str | None = Form(default=None),
                          user: User = Depends(steward), db: Session = Depends(get_db)):
    conn = _conn(db, name)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename or "upload.txt")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / safe
        path.write_bytes(await file.read())
        return _store_document(db, conn, path, safe, table or None)


@router.post("/connections/{name}/documents/raw", status_code=201)
async def upload_document_raw(name: str, request: Request, filename: str, table: str | None = None,
                              user: User = Depends(steward), db: Session = Depends(get_db)):
    """For Apache Hop (works on Vercel): the request body is the file itself, ?filename= names it."""
    conn = _conn(db, name)
    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty file.")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name or "upload.txt")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / safe
        path.write_bytes(data)
        return _store_document(db, conn, path, safe, table or None)


class DocPathIn(BaseModel):
    path: str
    table: str | None = None


@router.post("/connections/{name}/documents/from-path", status_code=201)
def document_from_path(name: str, body: DocPathIn, user: User = Depends(steward), db: Session = Depends(get_db)):
    """Docker only: register a file already placed in the shared docs folder."""
    conn = _conn(db, name)
    if not get_settings().shared_dir:
        raise HTTPException(400, "No shared folder on this deployment; use /documents/raw.")
    try:
        path = resolve_shared_path(get_settings().docs_dir, body.path)
    except DocumentError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _store_document(db, conn, path, path.name, body.table or None)


class ResearchIn(BaseModel):
    web_search: bool = False


@router.post("/connections/{name}/research")
async def research(name: str, body: ResearchIn, user: User = Depends(steward), db: Session = Depends(get_db)):
    """Step 8: run the context research agent; creates a new draft version."""
    conn = _conn(db, name)
    conn_id, conn_name, db_type, email = conn.id, conn.name, conn.db_type, user.email

    def run() -> dict:
        try:
            with telemetry.timed("api", name="research", user=email, connection=conn_name):
                out = run_research(conn_id, conn_name, db_type, email, body.web_search)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        with SessionLocal() as s2:
            c2 = get_connection(s2, conn_name)
            path = ctx.export_for_review(c2, ctx.get_version(s2, c2, out["version"]))
        if path:
            out["review_file"] = str(path)
        out["next_step"] = f"Run 08-review-draft with VERSION={out['version']}"
        return out

    return await keepalive.respond(lambda: asyncio.to_thread(run))


# ---------------------------------------------------------------- review, edit & approve
@router.get("/connections/{name}/versions")
def versions(name: str, user: User = Depends(steward), db: Session = Depends(get_db)):
    conn = _conn(db, name)
    rows = db.scalars(select(ContextVersion).where(ContextVersion.connection_id == conn.id)
                      .order_by(ContextVersion.version.desc())).all()
    return [ctx.version_summary(v) for v in rows]


def _version(db, name, version):
    conn = _conn(db, name)
    try:
        return conn, ctx.get_version(db, conn, version)
    except ctx.ContextError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/connections/{name}/versions/{version}")
def version_detail(name: str, version: str, user: User = Depends(steward), db: Session = Depends(get_db)):
    conn, v = _version(db, name, version)
    return ctx.version_summary(v) | {"metadata_layer": v.metadata_layer, "knowledge_layer": v.knowledge_layer,
                                     "research_log": v.research_log}


@router.get("/connections/{name}/versions/{version}/review")
def review_version(name: str, version: str, user: User = Depends(steward), db: Session = Depends(get_db)):
    """Editable review document. Edit it and POST it back to /edits/raw (or /edits)."""
    conn, v = _version(db, name, version)
    return ctx.review_document(conn, v)


@router.post("/connections/{name}/versions/{version}/export")
def export_version(name: str, version: str, user: User = Depends(steward), db: Session = Depends(get_db)):
    conn, v = _version(db, name, version)
    path = ctx.export_for_review(conn, v)
    if not path:
        raise HTTPException(400, "No shared folder on this deployment; use GET .../review.")
    return {"review_file": str(path)}


@router.post("/connections/{name}/versions/{version}/edits")
def edit_version(name: str, version: str, edits: dict, user: User = Depends(steward), db: Session = Depends(get_db)):
    conn, v = _version(db, name, version)
    try:
        return ctx.version_summary(ctx.apply_edits(db, v, edits, user.email))
    except ctx.ContextError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/connections/{name}/versions/{version}/edits/raw")
async def edit_version_raw(name: str, version: str, request: Request, user: User = Depends(steward),
                           db: Session = Depends(get_db)):
    """For Apache Hop: the body is the edited review file, sent as-is (any content type)."""
    conn, v = _version(db, name, version)
    try:
        edits = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, f"Review file is not valid JSON: {exc}") from exc
    try:
        return ctx.version_summary(ctx.apply_edits(db, v, edits, user.email))
    except ctx.ContextError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/connections/{name}/versions/{version}/edits/from-review-file")
def edits_from_file(name: str, version: str, user: User = Depends(steward), db: Session = Depends(get_db)):
    """Docker only: apply the edited review file written to the shared folder by /export."""
    conn, v = _version(db, name, version)
    if not get_settings().shared_dir:
        raise HTTPException(400, "No shared folder on this deployment; use /edits/raw.")
    path = get_settings().review_dir / f"{conn.name}-context-{v.version}.json"
    if not path.is_file():
        raise HTTPException(404, f"Review file not found: {path.name}")
    try:
        edits = json.loads(path.read_text())
        return ctx.version_summary(ctx.apply_edits(db, v, edits, user.email))
    except (json.JSONDecodeError, ctx.ContextError) as exc:
        raise HTTPException(400, f"Review file is invalid: {exc}") from exc


class DecisionIn(BaseModel):
    note: str = ""


@router.post("/connections/{name}/versions/{version}/approve")
def approve(name: str, version: str, body: DecisionIn, user: User = Depends(steward), db: Session = Depends(get_db)):
    conn, v = _version(db, name, version)
    try:
        return ctx.decide(db, conn, v, True, user.email, body.note)
    except ctx.ContextError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/connections/{name}/versions/{version}/reject")
def reject(name: str, version: str, body: DecisionIn, user: User = Depends(steward), db: Session = Depends(get_db)):
    conn, v = _version(db, name, version)
    try:
        return ctx.decide(db, conn, v, False, user.email, body.note)
    except ctx.ContextError as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------- Context API (consumed by agent & MCP)
@router.get("/context/{name}")
def context_api(name: str, version: str = "approved", user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    access = permitted(db, user).get(name)
    if access is None:
        raise HTTPException(403, f"No read access to '{name}'.")
    conn, v = _version(db, name, version)
    if v.status != "approved" and user.role not in ("steward", "admin"):
        raise HTTPException(403, "Only approved context is available to your role.")
    tables_ = None if access.tables is None else [t for t in v.metadata_layer["tables"] if access.allows_table(t)]
    return ctx.context_payload(conn, v, tables_, access.denied_columns)


# ---------------------------------------------------------------- Flow B: ask
class AskIn(BaseModel):
    question: str
    connection: str | None = None


@router.post("/ask")
async def ask(body: AskIn, user: User = Depends(current_user)):
    email = user.email

    async def run():
        try:
            return await answer_question(body.question, email, body.connection or None)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    return await keepalive.respond(run)


# ---------------------------------------------------------------- admin & access control
class UserIn(BaseModel):
    email: str
    department: str = ""
    role: str = "user"


@router.post("/admin/users", status_code=201)
def upsert_user(body: UserIn, user: User = Depends(admin), db: Session = Depends(get_db)):
    if body.role not in ("user", "steward", "admin"):
        raise HTTPException(400, "Role must be user, steward or admin.")
    u = ensure_user(db, body.email, body.role, body.department)
    u.role, u.department = body.role, body.department
    db.commit()
    return {"email": u.email, "role": u.role, "department": u.department}


@router.get("/admin/users")
def list_users(user: User = Depends(admin), db: Session = Depends(get_db)):
    return [{"email": u.email, "role": u.role, "department": u.department} for u in db.scalars(select(User))]


class PermissionIn(BaseModel):
    subject_type: str       # department | user
    subject: str
    connection: str
    tables: list[str] = Field(default_factory=list)
    denied_columns: list[str] = Field(default_factory=list)


@router.post("/admin/permissions", status_code=201)
def grant(body: PermissionIn, user: User = Depends(admin), db: Session = Depends(get_db)):
    if body.subject_type not in ("department", "user"):
        raise HTTPException(400, "subject_type must be 'department' or 'user'.")
    _conn(db, body.connection)
    p = Permission(subject_type=body.subject_type, subject=body.subject.lower() if body.subject_type == "user"
                   else body.subject, connection_name=body.connection, tables=body.tables,
                   denied_columns=body.denied_columns)
    db.add(p)
    db.commit()
    return {"id": p.id, "granted": "read", **body.model_dump()}


@router.get("/admin/permissions")
def list_permissions(user: User = Depends(admin), db: Session = Depends(get_db)):
    return [{"id": p.id, "subject_type": p.subject_type, "subject": p.subject, "connection": p.connection_name,
             "tables": p.tables, "denied_columns": p.denied_columns} for p in db.scalars(select(Permission))]


# ---------------------------------------------------------------- observability
@router.get("/observability/summary")
def obs_summary(hours: int = 24, user: User = Depends(steward)):
    return telemetry.summary(hours)


@router.get("/observability/events")
def obs_events(limit: int = 100, kind: str | None = None, user: User = Depends(steward),
               db: Session = Depends(get_db)):
    q = select(Telemetry).order_by(Telemetry.id.desc()).limit(min(limit, 1000))
    if kind:
        q = q.where(Telemetry.kind == kind)
    return [{"ts": t.ts, "kind": t.kind, "name": t.name, "user": t.user, "connection": t.connection,
             "status": t.status, "latency_ms": t.latency_ms, "tokens_in": t.tokens_in, "tokens_out": t.tokens_out,
             "trace_id": t.trace_id, "detail": t.detail} for t in db.scalars(q)]


# ---------------------------------------------------------------- internal: Cube data sources
internal = APIRouter(prefix="/internal")


def _internal(x_internal_token: str = Header(default="")) -> None:
    expected = get_settings().internal_token
    if not x_internal_token or not expected or not hmac.compare_digest(x_internal_token, expected):
        raise HTTPException(401, "Invalid internal token.")


@internal.get("/cube/models", dependencies=[Depends(_internal)])
def cube_models(db: Session = Depends(get_db)):
    """Model files for Cube's repositoryFactory."""
    return [{"fileName": f"{m.connection_name}.yml", "content": m.yaml} for m in db.scalars(select(CubeModel))]


@internal.get("/cube/schema-version", dependencies=[Depends(_internal)])
def cube_schema_version(db: Session = Depends(get_db)):
    return {"version": cube_model.schema_version(db)}


@internal.get("/cube/datasource-types", dependencies=[Depends(_internal)])
def cube_datasource_types(db: Session = Depends(get_db)):
    return {c.name: CONNECTORS[c.db_type].cube_type for c in db.scalars(select(Connection))}


@internal.get("/cube/datasources/{name}", dependencies=[Depends(_internal)])
def cube_datasource(name: str, db: Session = Depends(get_db)):
    conn = _conn(db, name)
    spec = CONNECTORS[conn.db_type]
    return {"type": spec.cube_type, "host": conn.host, "port": conn.port or spec.default_port,
            "database": conn.database, "user": conn.username, "password": crypto.decrypt(conn.secret_enc),
            "options": conn.options}
