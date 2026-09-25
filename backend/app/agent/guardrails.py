"""SQL and answer guardrails.

SQL: one statement, SELECT only (no insert / update / delete / merge / DDL / grants), only tables the
user may read, no denied columns, dangerous functions blocked, and a row limit always applied. This runs
in addition to the read-only database account and read-only session settings.
"""
import sqlglot
from sqlglot import exp

BLOCKED_FUNCTIONS = {"pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "lo_import", "lo_export",
                     "dblink", "dblink_exec", "sleep", "benchmark", "load_file", "xp_cmdshell", "openrowset",
                     "utl_http", "utl_file", "system$", "sys_exec"}
WRITE_NODES = tuple(getattr(exp, n) for n in (
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterTable", "TruncateTable", "Grant",
    "Revoke", "Command", "Copy", "Set", "Use", "Transaction", "Commit", "Rollback", "LoadData", "Pragma")
    if hasattr(exp, n))


class GuardrailViolation(ValueError):
    pass


def _table_name(t: exp.Table) -> str:
    return f"{t.db}.{t.name}" if t.db else t.name


def validate_select(sql: str, dialect: str, allowed_tables: set[str] | None, denied_columns: set[str],
                    known_tables: set[str], limit: int) -> str:
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise GuardrailViolation(f"SQL could not be parsed: {str(exc).splitlines()[0]}") from exc
    if len(statements) != 1:
        raise GuardrailViolation("Exactly one SQL statement is allowed.")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise GuardrailViolation("Only read-only SELECT queries are allowed.")
    for node in tree.walk():
        if isinstance(node, WRITE_NODES):
            raise GuardrailViolation("Write, DDL and session statements are blocked (read-only access).")
        if isinstance(node, exp.Anonymous) and str(node.name).lower() in BLOCKED_FUNCTIONS:
            raise GuardrailViolation(f"Function {node.name} is blocked.")
        if isinstance(node, exp.Func) and node.sql_name().lower() in BLOCKED_FUNCTIONS:
            raise GuardrailViolation(f"Function {node.sql_name()} is blocked.")
        if isinstance(node, exp.Into):
            raise GuardrailViolation("SELECT ... INTO is blocked.")

    cte_names = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    short_known = {k.split(".")[-1].lower(): k for k in known_tables}
    used: set[str] = set()
    for t in tree.find_all(exp.Table):
        name = _table_name(t)
        if t.name in cte_names and not t.db:
            continue
        canonical = name if name in known_tables else short_known.get(t.name.lower())
        if canonical is None:
            raise GuardrailViolation(f"Unknown table '{name}'. Use list_tables to see available tables.")
        if allowed_tables is not None and canonical not in allowed_tables and \
                canonical.split(".")[-1] not in {a.split(".")[-1] for a in allowed_tables}:
            raise GuardrailViolation(f"You do not have read access to '{canonical}'.")
        used.add(canonical)

    if denied_columns:
        denied_short = {d.rsplit(".", 1)[-1].lower(): d for d in denied_columns
                        if d.rsplit(".", 1)[0] in used or d.rsplit(".", 1)[0].split(".")[-1] in
                        {u.split(".")[-1] for u in used}}
        if denied_short and any(isinstance(s, exp.Star) for s in tree.find_all(exp.Star)):
            raise GuardrailViolation("SELECT * is not allowed on tables with restricted columns; list the columns.")
        for col in tree.find_all(exp.Column):
            if col.name.lower() in denied_short:
                raise GuardrailViolation(f"Column '{col.name}' is restricted for your account.")

    if isinstance(tree, exp.Select):
        current = tree.args.get("limit")
        current_n = None
        if current is not None:
            try:
                current_n = int(current.expression.name)
            except (AttributeError, ValueError):
                current_n = None
        if current_n is None or current_n > limit:
            tree = tree.limit(limit)
    else:
        tree = exp.select("*").from_(tree.subquery("q")).limit(limit)
    return tree.sql(dialect=dialect)


def tables_in(sql: str, dialect: str) -> list[str]:
    try:
        return sorted({_table_name(t) for t in sqlglot.parse_one(sql, read=dialect).find_all(exp.Table)})
    except sqlglot.errors.ParseError:
        return []
