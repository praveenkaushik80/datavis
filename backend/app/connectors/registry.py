"""Connector registry: one entry per supported client database.

Each entry maps the DataFusion db_type to a SQLAlchemy dialect (verification, catalogue, MCP read-only
queries), an OpenMetadata service type (catalog ingestion) and a Cube driver type (semantic layer).
"""
from dataclasses import dataclass, field
from urllib.parse import quote_plus

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class ConnectorSpec:
    db_type: str
    label: str
    release: str                 # MVP1 | MVP2 | dev
    sqlalchemy_driver: str
    default_port: int | None
    om_service_type: str | None
    cube_type: str | None
    pip_package: str
    sqlglot_dialect: str
    extra_options: tuple = field(default_factory=tuple)


CONNECTORS: dict[str, ConnectorSpec] = {
    "postgres": ConnectorSpec("postgres", "PostgreSQL", "MVP1", "postgresql+psycopg", 5432, "Postgres", "postgres",
                              "psycopg[binary]", "postgres"),
    "mysql": ConnectorSpec("mysql", "MySQL", "MVP2", "mysql+pymysql", 3306, "Mysql", "mysql", "pymysql", "mysql"),
    "mssql": ConnectorSpec("mssql", "SQL Server", "MVP2", "mssql+pymssql", 1433, "Mssql", "mssql", "pymssql", "tsql"),
    "oracle": ConnectorSpec("oracle", "Oracle", "MVP2", "oracle+oracledb", 1521, "Oracle", "oracle", "oracledb",
                            "oracle", ("service_name",)),
    "snowflake": ConnectorSpec("snowflake", "Snowflake", "MVP2", "snowflake", None, "Snowflake", "snowflake",
                               "snowflake-sqlalchemy", "snowflake", ("account", "warehouse", "role", "schema")),
    # Local development / tests only
    "sqlite": ConnectorSpec("sqlite", "SQLite (dev only)", "dev", "sqlite", None, None, "sqlite", "", "sqlite"),
}


class UnsupportedConnector(ValueError):
    pass


def spec_for(db_type: str) -> ConnectorSpec:
    try:
        return CONNECTORS[db_type]
    except KeyError as exc:
        raise UnsupportedConnector(
            f"Unsupported database type '{db_type}'. Choose one of: {', '.join(CONNECTORS)}") from exc


def build_url(db_type: str, host: str, port: int | None, database: str, username: str, password: str,
              options: dict | None = None) -> str:
    spec = spec_for(db_type)
    options = options or {}
    if db_type == "sqlite":
        return f"sqlite:///{database}"
    user = quote_plus(username or "")
    pwd = quote_plus(password or "")
    auth = f"{user}:{pwd}@" if username else ""
    if db_type == "snowflake":
        account = options.get("account") or host
        url = f"snowflake://{auth}{account}/{database}"
        params = [f"{k}={quote_plus(str(options[k]))}" for k in ("schema", "warehouse", "role") if options.get(k)]
        return url + ("?" + "&".join(params) if params else "")
    if db_type == "oracle" and options.get("service_name"):
        return f"{spec.sqlalchemy_driver}://{auth}{host}:{port or spec.default_port}/?service_name={options['service_name']}"
    return f"{spec.sqlalchemy_driver}://{auth}{host}:{port or spec.default_port}/{database}"


def read_only_engine(db_type: str, url: str, timeout_seconds: int) -> Engine:
    """Engine that is read-only at the session level where the database supports it.

    SQL guardrails (sqlglot, SELECT only) run before every query as a second layer, and the database
    account itself should be read-only: defence in depth.
    """
    connect_args: dict = {}
    if db_type == "postgres":
        connect_args["options"] = (f"-c default_transaction_read_only=on "
                                   f"-c statement_timeout={timeout_seconds * 1000}")
    if db_type == "mysql":
        connect_args["read_timeout"] = timeout_seconds
    if db_type == "sqlite":
        # open the file read-only
        path = url.replace("sqlite:///", "")
        url = f"sqlite:///file:{path}?mode=ro&uri=true"
    eng = create_engine(url, connect_args=connect_args, pool_pre_ping=True)

    if db_type == "mysql":
        @event.listens_for(eng, "connect")
        def _mysql_ro(dbapi_conn, _):  # pragma: no cover - needs MySQL
            cur = dbapi_conn.cursor()
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute(f"SET SESSION MAX_EXECUTION_TIME={timeout_seconds * 1000}")
            cur.close()
    return eng
