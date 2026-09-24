from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

ALLOWED_RELATIONS: frozenset[str] = frozenset(
    {"sem.v_claims", "sem.v_payments_net", "sem.v_premium", "sem.v_claim_detail", "agg.metric"}
)

ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "round",
        "coalesce",
        "nullif",
        "date_trunc",
        "extract",
        "date_part",
        "abs",
        "greatest",
        "least",
        "lower",
        "upper",
        "to_char",
    }
)

# Exact names, then prefixes for the families. Checked before the allow-list so a blocked name is
# never waved through by also being spellable as something allowed.
_DENIED_EXACT: frozenset[str] = frozenset({"set_config", "current_setting", "pg_logical_emit_message", "pg_notify"})
_DENIED_PREFIXES: tuple[str, ...] = (
    "pg_sleep",
    "pg_read_file",
    "pg_ls_dir",
    "lo_",
    "dblink",
    "txid_",
    "pg_advisory",
)

_SYSTEM_COLUMNS: frozenset[str] = frozenset({"ctid", "xmin", "xmax", "cmin", "cmax", "tableoid"})
_BANNED_SCHEMAS: frozenset[str] = frozenset({"pg_catalog", "information_schema"})

# Statement kinds that must never appear, at the root or nested in a CTE or subquery.
_FORBIDDEN: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Copy,
    exp.Command,
    exp.Set,
    exp.SetItem,
    exp.Grant,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Use,
    exp.Pragma,
    exp.Into,
    exp.Lock,
)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None
    sql: str


def check(
    sql: str,
    *,
    relations: frozenset[str] = ALLOWED_RELATIONS,
    functions: frozenset[str] | None = None,
    max_limit: int = 500,
) -> Verdict:
    # Defence in depth. The login role's grants and row-level security are the real boundary;
    # this keeps a well-formed query on the allowed shape and never sees a row.
    allowed_functions = ALLOWED_FUNCTIONS if functions is None else functions
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except (SqlglotError, RecursionError):
        return _no(sql, "not valid SQL")

    if len(statements) != 1:
        return _no(sql, "expected a single statement")
    root = statements[0]
    if not isinstance(root, exp.Select):
        return _no(sql, "only a single SELECT is allowed")

    reason = _inspect(root, relations, allowed_functions)
    if reason is not None:
        return _no(sql, reason)

    _apply_limit(root, max_limit)
    return Verdict(ok=True, reason=None, sql=root.sql(dialect="postgres", comments=False))


def _inspect(root: exp.Select, relations: frozenset[str], allowed_functions: frozenset[str]) -> str | None:
    for node in root.walk():
        if isinstance(node, _FORBIDDEN):
            return "only a read-only SELECT is allowed"

    cte_names = {cte.alias.lower() for cte in root.find_all(exp.CTE)}
    bare = {name.split(".")[-1] for name in relations}
    for name in cte_names:
        if name in relations or name in bare:
            return f"CTE name shadows a relation: {name}"

    for column in root.find_all(exp.Column):
        if column.name.lower() in _SYSTEM_COLUMNS:
            return f"system column is not allowed: {column.name.lower()}"

    for dot in root.find_all(exp.Dot):
        if isinstance(dot.expression, exp.Anonymous):
            return "schema-qualified function calls are not allowed"

    for anon in root.find_all(exp.Anonymous):
        name = anon.name.lower()
        if _denied(name):
            return f"function is blocked: {name}"
        # A table function (agg.metric) is vetted as a relation, not as a scalar call.
        if isinstance(anon.parent, exp.Table):
            continue
        if name not in allowed_functions:
            return f"function is not allowed: {name}"

    for table in root.find_all(exp.Table):
        reason = _check_relation(table, relations, cte_names)
        if reason is not None:
            return reason
    return None


def _check_relation(table: exp.Table, relations: frozenset[str], cte_names: set[str]) -> str | None:
    if table.args.get("catalog"):
        return "cross-database references are not allowed"
    schema = table.args.get("db")
    schema_name = schema.name.lower() if schema else None

    if isinstance(table.this, exp.Anonymous):
        func = table.this.name.lower()
        qualified = f"{schema_name}.{func}" if schema_name else func
        if qualified not in relations:
            return f"relation is not allowed: {qualified}"
        return None

    name = table.name.lower()
    if schema_name is None:
        if name in cte_names:
            return None
        return f"relation must be schema-qualified: {name}"
    if schema_name in _BANNED_SCHEMAS:
        return f"schema is not allowed: {schema_name}"
    qualified = f"{schema_name}.{name}"
    if qualified not in relations:
        return f"relation is not allowed: {qualified}"
    return None


def _denied(name: str) -> bool:
    return name in _DENIED_EXACT or name.startswith(_DENIED_PREFIXES) or name.endswith("_to_xml")


def _apply_limit(root: exp.Select, max_limit: int) -> None:
    root.set("limit", exp.Limit(expression=exp.Literal.number(min(_current_limit(root), max_limit))))


def _current_limit(root: exp.Select) -> int:
    node = root.args.get("limit")
    literal: exp.Expression | None = None
    if isinstance(node, exp.Limit):
        literal = node.expression
    elif isinstance(node, exp.Fetch):
        literal = node.args.get("count")
    if isinstance(literal, exp.Literal) and not literal.is_string:
        return int(literal.name)
    # No limit, or one we cannot read (a placeholder or subquery); clamp to the cap.
    return 1 << 62


def _no(sql: str, reason: str) -> Verdict:
    return Verdict(ok=False, reason=reason, sql=sql)
