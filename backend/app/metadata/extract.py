"""Built-in metadata catalog pipeline (runs automatically once connected).

Reads schemas, tables, columns, types, keys, comments, row counts and light profiles using catalogue and
statistics queries only. No row data is copied into DataFusion. When OpenMetadata is configured, the same
metadata is also ingested there and OpenMetadata becomes the catalog of record.
"""
from sqlalchemy import case, column as sa_column, create_engine, func, inspect, select, table as sa_table, text
from sqlalchemy.orm import Session

from ..connectors.registry import build_url
from ..connectors.verify import SYSTEM_SCHEMAS
from ..db import CatalogTable, Connection
from ..security import crypto

PROFILE_ROW_LIMIT = 100_000   # only profile columns on tables at or below this size


def _row_count(conn, db_type: str, schema: str, name: str) -> int | None:
    try:
        if db_type == "postgres":
            v = conn.execute(text("SELECT reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                                  "WHERE n.nspname=:s AND c.relname=:t"), {"s": schema, "t": name}).scalar()
            if v is not None and v >= 0:
                return int(v)
        t = sa_table(name, schema=None if db_type == "sqlite" else schema)
        return int(conn.execute(select(func.count()).select_from(t)).scalar())
    except Exception:  # noqa: BLE001
        return None


def _profile(conn, db_type: str, schema: str, name: str, columns: list[dict]) -> None:
    """Null ratio and distinct count per column: statistics only, no values stored."""
    t = sa_table(name, *[sa_column(c["name"]) for c in columns], schema=None if db_type == "sqlite" else schema)
    for c in columns:
        col = t.c[c["name"]]
        try:
            total, nulls, distinct = conn.execute(select(
                func.count(), func.sum(case((col.is_(None), 1), else_=0)),
                func.count(func.distinct(col))).select_from(t)).one()
            c["profile"] = {"null_ratio": round((nulls or 0) / total, 4) if total else None,
                            "distinct": int(distinct or 0)}
        except Exception:  # noqa: BLE001 - some types (json, blobs) cannot be profiled
            c["profile"] = {}


def extract_catalog(db: Session, connection: Connection, profile: bool = True) -> dict:
    password = crypto.decrypt(connection.secret_enc)
    eng = create_engine(build_url(connection.db_type, connection.host, connection.port, connection.database,
                                  connection.username, password, connection.options))
    insp = inspect(eng)
    if connection.db_type == "sqlite":
        schemas = [""]
    else:
        schemas = connection.options.get("schemas") or [s for s in insp.get_schema_names() if s not in SYSTEM_SCHEMAS]
    existing = {(t.schema_name, t.table_name): t for t in connection.tables}
    seen, count = set(), 0
    with eng.connect() as conn:
        for schema in schemas:
            sch = schema or None
            for name in insp.get_table_names(schema=sch):
                cols_raw = insp.get_columns(name, schema=sch)
                pk = set((insp.get_pk_constraint(name, schema=sch) or {}).get("constrained_columns") or [])
                fks = [{"columns": fk["constrained_columns"],
                        "ref_table": (f"{fk['referred_schema']}.{fk['referred_table']}" if fk.get("referred_schema")
                                      else fk["referred_table"]),
                        "ref_columns": fk["referred_columns"]} for fk in insp.get_foreign_keys(name, schema=sch)]
                try:
                    tcomment = (insp.get_table_comment(name, schema=sch) or {}).get("text") or ""
                except NotImplementedError:
                    tcomment = ""
                columns = [{"name": c["name"], "type": str(c["type"]), "nullable": bool(c.get("nullable", True)),
                            "comment": c.get("comment") or "", "pk": c["name"] in pk} for c in cols_raw]
                rows = _row_count(conn, connection.db_type, schema, name)
                if profile and rows is not None and rows <= PROFILE_ROW_LIMIT:
                    _profile(conn, connection.db_type, schema, name, columns)
                key = (schema, name)
                row = existing.get(key) or CatalogTable(connection_id=connection.id, schema_name=schema, table_name=name)
                row.columns, row.foreign_keys, row.row_count = columns, fks, rows
                row.description = row.description or tcomment
                db.add(row)
                seen.add(key)
                count += 1
    for key, row in existing.items():
        if key not in seen:
            db.delete(row)
    connection.status = "catalogued"
    db.commit()
    eng.dispose()
    return {"tables": count, "schemas": [s or "main" for s in schemas]}
