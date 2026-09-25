"""RBAC, read access only: department / user -> databases, tables, columns."""
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import Connection, Permission, User


@dataclass
class Access:
    tables: set[str] | None = None            # None = every table
    denied_columns: set[str] = field(default_factory=set)

    def allows_table(self, table: str) -> bool:
        return self.tables is None or table in self.tables or table.split(".")[-1] in {
            t.split(".")[-1] for t in self.tables}


def permitted(db: Session, user: User) -> dict[str, Access]:
    if user.role in ("admin", "steward"):
        return {c.name: Access() for c in db.scalars(select(Connection))}
    grants = db.scalars(select(Permission).where(or_(
        (Permission.subject_type == "user") & (Permission.subject == user.email),
        (Permission.subject_type == "department") & (Permission.subject == user.department)))).all()
    out: dict[str, Access] = {}
    for g in grants:
        acc = out.setdefault(g.connection_name, Access(tables=set()))
        if not g.tables:
            acc.tables = None
        elif acc.tables is not None:
            acc.tables.update(g.tables)
        acc.denied_columns.update(g.denied_columns or [])
    return out
