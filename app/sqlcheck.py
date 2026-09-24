import string
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

ALLOWED_RELATIONS: frozenset[str] = frozenset(
    {"sem.v_claims", "sem.v_payments_net", "sem.v_premium", "sem.v_claim_detail", "agg.metric"}
)

# Exactly the functions the compiler and the app's fixed queries call, which a test enumerates and holds this
# equal to. A measure that needs another fails that test until the name is added here on purpose.
ALLOWED_FUNCTIONS: frozenset[str] = frozenset({"count", "sum", "coalesce", "nullif", "date_trunc"})

# Types a cast or typed literal may name, where FLOAT is real and DOUBLE is double precision. Everything else
# is refused, above all oid and the reg* family, which resolve a name against the catalog.
ALLOWED_TYPES: frozenset[exp.DType] = frozenset(
    {
        exp.DType.SMALLINT,
        exp.DType.INT,
        exp.DType.BIGINT,
        exp.DType.DECIMAL,
        exp.DType.FLOAT,
        exp.DType.DOUBLE,
        exp.DType.TEXT,
        exp.DType.VARCHAR,
        exp.DType.CHAR,
        exp.DType.BOOLEAN,
        exp.DType.DATE,
        exp.DType.TIMESTAMP,
        exp.DType.TIMESTAMPTZ,
        exp.DType.INTERVAL,
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
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_TYPE_SLOTS: tuple[str, ...] = ("return_type", "returning")

# Syntax that sqlglot models as a function but that calls nothing. BETWEEN, IN, LIKE, IS, NOT, ANY and the
# arithmetic and comparison operators are not Func nodes at all, so they never reach the call check.
_OPERATORS: tuple[type[exp.Func], ...] = (exp.Cast, exp.Case, exp.If, exp.And, exp.Or, exp.Exists)

# Where sqlglot's own name for a node differs from the name the postgres dialect writes for it.
_PG_NAMES: dict[type[exp.Func], str] = {
    exp.TimestampTrunc: "date_trunc",
    exp.TimeToStr: "to_char",
    exp.GroupConcat: "string_agg",
    exp.CurrentVersion: "version",
    exp.ExplodingGenerateSeries: "generate_series",
    exp.JSONObject: "json_object",
}

# Calls Postgres makes without parentheses. sqlglot reads some of them, such as user, as a column.
_NILADIC: frozenset[str] = frozenset(
    {
        "current_catalog",
        "current_date",
        "current_role",
        "current_schema",
        "current_time",
        "current_timestamp",
        "current_user",
        "localtime",
        "localtimestamp",
        "session_user",
        "system_user",
        "user",
    }
)

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
    root = _single_select(sql)
    if isinstance(root, str):
        return _no(sql, root)
    reason = _inspect(root, relations, allowed_functions)
    if reason is not None:
        return _no(sql, reason)

    _apply_limit(root, max_limit)
    rendered = root.sql(dialect="postgres", comments=False)
    # The regenerated SQL is what runs, and generation can write what the input never held, as PARSE_JSON
    # comes out as a CAST to json. So the output is parsed and inspected in its turn.
    again = _single_select(rendered)
    reason = again if isinstance(again, str) else _inspect(again, relations, allowed_functions)
    if reason is not None:
        return _no(sql, reason)
    return Verdict(ok=True, reason=None, sql=rendered)


def _single_select(sql: str) -> exp.Select | str:
    """The one SELECT the text holds, or why it is refused."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except (SqlglotError, RecursionError):
        return "not valid SQL"
    if len(statements) != 1:
        return "expected a single statement"
    if not isinstance(statements[0], exp.Select):
        return "only a single SELECT is allowed"
    return statements[0]


def _inspect(root: exp.Select, relations: frozenset[str], allowed_functions: frozenset[str]) -> str | None:
    for node in root.walk():
        if isinstance(node, _FORBIDDEN):
            return "only a read-only SELECT is allowed"
        if isinstance(node, exp.With) and node.recursive:
            return "recursive CTEs are not allowed"
        # The JSON functions keep a RETURNING type as a bare name, out of reach of the type check below.
        for key in _TYPE_SLOTS:
            slot = node.args.get(key)
            if isinstance(slot, exp.Expr) and not isinstance(slot, exp.DataType):
                return f"RETURNING type is not allowed: {str(slot).lower()}"

    # Every allowed relation, not only the ones this call may read.
    shadowed = relations | ALLOWED_RELATIONS
    bare = {name.split(".")[-1] for name in shadowed}
    for cte in root.find_all(exp.CTE):
        name = cte.alias.lower()
        if name in shadowed or name in bare:
            return f"CTE name shadows a relation: {name}"

    for column in root.find_all(exp.Column):
        if column.name.lower() in _SYSTEM_COLUMNS:
            return f"system column is not allowed: {column.name.lower()}"

    for cast in root.find_all(exp.Cast, exp.CastToStrType):
        if not isinstance(cast.args.get("to"), exp.DataType):
            return "a cast must name a type"
    for dtype in root.find_all(exp.DataType):
        if not _type_allowed(dtype):
            return f"type is not allowed: {dtype.sql(dialect='postgres').lower()}"

    for dot in root.find_all(exp.Dot):
        if isinstance(dot.expression, exp.Func):
            return "schema-qualified function calls are not allowed"

    # Every call, whether sqlglot knows the function or not, so anything off the list is refused.
    for node in root.find_all(exp.Func, exp.Column):
        called = function_name(node)
        if called is None:
            continue
        if _denied(called):
            return f"function is blocked: {called}"
        # A table function (agg.metric) is vetted as a relation, not as a scalar call.
        if isinstance(node.parent, exp.Table) and node.arg_key == "this":
            continue
        if called not in allowed_functions:
            return f"function is not allowed: {called}"

    for table in root.find_all(exp.Table):
        reason = _check_relation(table, relations)
        if reason is not None:
            return reason
    return None


def _type_allowed(dtype: exp.DataType) -> bool:
    # oid, the reg* family and the pseudo-types parse as subclasses of DataType.
    if type(dtype) is not exp.DataType:
        return False
    # An array's element type is a DataType node of its own and is checked in its turn.
    if dtype.this == exp.DType.ARRAY:
        return len(dtype.expressions) == 1 and isinstance(dtype.expressions[0], exp.DataType)
    return dtype.this in ALLOWED_TYPES or _jsonb_argument(dtype)


def _jsonb_argument(dtype: exp.DataType) -> bool:
    # agg.metric takes its filters as jsonb, so a bound parameter passed straight to it may be cast to jsonb.
    cast = dtype.parent
    return (
        dtype.this == exp.DType.JSONB
        and isinstance(cast, exp.Cast)
        and isinstance(cast.this, exp.Placeholder)
        and isinstance(cast.parent, exp.Anonymous)
        and isinstance(cast.parent.parent, exp.Table)
    )


def _check_relation(table: exp.Table, relations: frozenset[str]) -> str | None:
    if table.args.get("catalog"):
        return "cross-database references are not allowed"
    schema = table.args.get("db")
    schema_name = schema.name.lower() if schema else None

    if isinstance(table.this, exp.Func):
        func = function_name(table.this) or type(table.this).__name__.lower()
        qualified = f"{schema_name}.{func}" if schema_name else func
        if qualified not in relations:
            return f"relation is not allowed: {qualified}"
        return None

    name = table.name.lower()
    if schema_name is None:
        if _folded(table.this) in _ctes_in_scope(table):
            return None
        return f"relation must be schema-qualified: {name}"
    if schema_name in _BANNED_SCHEMAS:
        return f"schema is not allowed: {schema_name}"
    qualified = f"{schema_name}.{name}"
    if qualified not in relations:
        return f"relation is not allowed: {qualified}"
    return None


def _ctes_in_scope(node: exp.Expr) -> set[str]:
    # A WITH covers its own query and everything nested in it. A CTE body sees only the CTEs declared
    # before it, since without RECURSIVE a WITH never sees itself or what follows.
    visible: list[exp.Expr] = []
    child, parent = node, node.parent
    while parent is not None:
        if isinstance(parent, exp.With):
            visible += parent.expressions[: child.index or 0]
        elif isinstance(with_ := parent.args.get("with_"), exp.With) and child is not with_:
            visible += with_.expressions
        child, parent = parent, parent.parent
    return {name for cte in visible if (name := _folded(cte.args["alias"].this)) is not None}


def _folded(node: object) -> str | None:
    # Postgres lowers only the ASCII letters of an unquoted name, so the Kelvin sign stays and never
    # becomes k as it would under str.lower(). A quoted name matches exactly.
    if not isinstance(node, exp.Identifier):
        return None
    return node.name if node.quoted else node.name.translate(_ASCII_LOWER)


def function_name(node: exp.Expr) -> str | None:
    """The name Postgres runs a call under, or None when the node calls nothing."""
    if isinstance(node, exp.Column):
        ident = node.this
        if node.table or not isinstance(ident, exp.Identifier) or ident.quoted:
            return None
        name = ident.name.translate(_ASCII_LOWER)
        return name if name in _NILADIC else None
    if not isinstance(node, exp.Func) or isinstance(node, _OPERATORS):
        return None
    if isinstance(node, (exp.Anonymous, exp.AnonymousAggFunc)):
        return node.name.lower()
    return _PG_NAMES.get(type(node), node.sql_name().lower())


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
