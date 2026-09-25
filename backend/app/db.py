"""Application database: the system of record for DataFusion's own state."""
from datetime import datetime, timezone

import logging
import threading
import time

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import NullPool

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Connection(Base):
    """A client database registered through the connection wizard (Flow A, ribbon 1)."""
    __tablename__ = "connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    db_type: Mapped[str] = mapped_column(String(32))
    host: Mapped[str] = mapped_column(String(255), default="")
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    database: Mapped[str] = mapped_column(String(255), default="")
    username: Mapped[str] = mapped_column(String(255), default="")
    secret_enc: Mapped[str] = mapped_column(Text, default="")        # Fernet-encrypted password
    options: Mapped[dict] = mapped_column(JSON, default=dict)         # schemas, warehouse, account, ...
    status: Mapped[str] = mapped_column(String(32), default="registered")
    om_service: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    tables: Mapped[list["CatalogTable"]] = relationship(back_populates="connection", cascade="all, delete-orphan")


class CatalogTable(Base):
    """Standard metadata for one table, mirrored from OpenMetadata or the built-in extractor."""
    __tablename__ = "catalog_tables"
    __table_args__ = (UniqueConstraint("connection_id", "schema_name", "table_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"))
    schema_name: Mapped[str] = mapped_column(String(255), default="")
    table_name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    columns: Mapped[list] = mapped_column(JSON, default=list)        # [{name,type,nullable,comment,pk,profile}]
    foreign_keys: Mapped[list] = mapped_column(JSON, default=list)   # [{columns,ref_table,ref_columns}]
    om_fqn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    connection: Mapped[Connection] = relationship(back_populates="tables")

    @property
    def qualified(self) -> str:
        return f"{self.schema_name}.{self.table_name}" if self.schema_name else self.table_name


class StewardNote(Base):
    """Business meaning and requirements a user adds on the table / column page."""
    __tablename__ = "steward_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"))
    table_name: Mapped[str] = mapped_column(String(512))
    column_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    business_meaning: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Document(Base):
    """Supporting document text (PDF, Word, JSON, TXT, CSV) passed straight to the agent (no RAG in MVP1)."""
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"))
    filename: Mapped[str] = mapped_column(String(512))
    table_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ContextVersion(Base):
    """A versioned semantic context: metadata layer + knowledge base layer. Every version is kept."""
    __tablename__ = "context_versions"
    __table_args__ = (UniqueConstraint("connection_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="draft")   # draft | approved | rejected | superseded
    metadata_layer: Mapped[dict] = mapped_column(JSON, default=dict)
    knowledge_layer: Mapped[dict] = mapped_column(JSON, default=dict)
    research_log: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str] = mapped_column(Text, default="")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    department: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[str] = mapped_column(String(16), default="user")      # user | steward | admin


class OtpCode(Base):
    __tablename__ = "otp_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255))
    code_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(default=False)


class Permission(Base):
    """Read access grant: a department or user -> a database, optionally narrowed to tables / columns."""
    __tablename__ = "permissions"
    id: Mapped[int] = mapped_column(primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(16))              # department | user
    subject: Mapped[str] = mapped_column(String(255))
    connection_name: Mapped[str] = mapped_column(String(64))
    tables: Mapped[list] = mapped_column(JSON, default=list)           # [] = all tables
    denied_columns: Mapped[list] = mapped_column(JSON, default=list)   # ["schema.table.column"]


class Telemetry(Base):
    __tablename__ = "telemetry"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(String(32))                      # api | agent | tool | llm | error
    user: Mapped[str] = mapped_column(String(255), default="")
    connection: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="ok")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class CubeModel(Base):
    """Cube data model generated from the approved semantic context. Cube reads it through the internal
    API (repositoryFactory in cube.js), so Cube needs no shared disk and can run anywhere."""
    __tablename__ = "cube_models"
    connection_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    context_version: Mapped[int] = mapped_column(Integer)
    yaml: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


log = logging.getLogger(__name__)
_engine = None
_Session = None
_lock = threading.Lock()


def _create_schema(eng) -> None:
    """Idempotent. Concurrent cold starts on a fresh database can race; retry once they settle."""
    for attempt in range(5):
        try:
            Base.metadata.create_all(eng)
            return
        except (OperationalError, ProgrammingError) as exc:
            if attempt == 4:
                raise
            log.warning("schema creation retry %s: %s", attempt + 1, str(exc).splitlines()[0])
            time.sleep(0.5 * (attempt + 1))


def engine():
    global _engine, _Session
    if _engine is None:
        with _lock:
            if _engine is None:
                s = get_settings()
                url = s.database_url
                if url.startswith("sqlite"):
                    kwargs = {"connect_args": {"check_same_thread": False}}
                elif s.serverless:
                    # Serverless instances come and go: no client-side pool; use the provider's pooler
                    # (e.g. Neon's -pooler host) for connection reuse.
                    kwargs = {"poolclass": NullPool}
                else:
                    kwargs = {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 10}
                eng = create_engine(url, **kwargs)
                _create_schema(eng)
                _Session = sessionmaker(bind=eng, expire_on_commit=False)
                _engine = eng
    return _engine


def SessionLocal():
    engine()
    return _Session()


def init_db() -> None:
    engine()


def reset_engine() -> None:
    """Used by tests after changing settings."""
    global _engine, _Session
    _engine, _Session = None, None
