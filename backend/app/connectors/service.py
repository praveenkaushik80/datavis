"""Connection lifecycle: register (verify -> encrypt -> store), look up, build read-only engines."""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import Connection
from ..security import crypto
from .registry import build_url, read_only_engine, spec_for
from .verify import verify_connection

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")


class ConnectionError_(ValueError):
    pass


def register_connection(db: Session, *, name: str, db_type: str, host: str, port: int | None, database: str,
                        username: str, password: str, options: dict | None = None) -> tuple[Connection, dict]:
    if not NAME_RE.match(name):
        raise ConnectionError_("Name must be lowercase letters, digits and underscores, starting with a letter.")
    if db.scalar(select(Connection).where(Connection.name == name)):
        raise ConnectionError_(f"A connection named '{name}' already exists.")
    spec_for(db_type)
    check = verify_connection(db_type, host, port, database, username, password, options)
    if not check["ok"]:
        raise ConnectionError_(check["error"])
    conn = Connection(name=name, db_type=db_type, host=host, port=port, database=database, username=username,
                      secret_enc=crypto.encrypt(password), options=options or {}, status="verified")
    db.add(conn)
    db.commit()
    return conn, check


def get_connection(db: Session, name: str) -> Connection:
    conn = db.scalar(select(Connection).where(Connection.name == name))
    if not conn:
        raise ConnectionError_(f"Unknown connection '{name}'.")
    return conn


def connection_url(conn: Connection) -> str:
    return build_url(conn.db_type, conn.host, conn.port, conn.database, conn.username,
                     crypto.decrypt(conn.secret_enc), conn.options)


def readonly_engine_for(conn: Connection):
    return read_only_engine(conn.db_type, connection_url(conn), get_settings().query_timeout_seconds)


def public_view(conn: Connection) -> dict:
    spec = spec_for(conn.db_type)
    return {"name": conn.name, "db_type": conn.db_type, "label": spec.label, "release": spec.release,
            "host": conn.host, "port": conn.port, "database": conn.database, "username": conn.username,
            "password": crypto.mask(conn.secret_enc), "options": conn.options, "status": conn.status,
            "openmetadata_service": conn.om_service, "table_count": len(conn.tables)}
