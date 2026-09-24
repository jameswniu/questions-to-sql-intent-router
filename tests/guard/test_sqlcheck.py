import pytest
import sqlglot
from sqlglot import exp

from app.config import ROOT
from app.sqlcheck import ALLOWED_FUNCTIONS, ALLOWED_RELATIONS, check, function_name

_DASHES = chr(45) * 2
_SEPARATOR = _DASHES + " next"
_COMMENT = _DASHES + " "


def _hostile_statements() -> list[str]:
    text = (ROOT / "tests" / "data" / "hostile_sql.sql").read_text()
    statements: list[str] = []
    for block in text.split(_SEPARATOR):
        lines = [ln for ln in block.splitlines() if not ln.lstrip().startswith(_COMMENT)]
        stmt = "\n".join(lines).strip()
        if stmt:
            statements.append(stmt)
    return statements


HOSTILE = _hostile_statements()

ALLOWED = [
    "SELECT claim_id, region FROM sem.v_claims WHERE region = %s",
    "SELECT date_trunc('month', paid_date) AS m, sum(amount) FROM sem.v_payments_net GROUP BY 1 ORDER BY 1",
    "SELECT * FROM agg.metric(%s, %s, %s::jsonb, %s, %s)",
    "WITH recent AS (SELECT * FROM sem.v_claims WHERE status = 'open') SELECT count(*) FROM recent",
    "SELECT sum(paid_total)::numeric / nullif(count(*), 0), count(DISTINCT claim_id) FROM sem.v_claims",
    "SELECT * FROM sem.v_claim_detail WHERE claim_id = %(cid)s",
    "SELECT date_trunc('quarter', loss_date)::date, count(*) FILTER (WHERE status = 'denied') FROM sem.v_claims",
    "SELECT coalesce(sum(amount), 0) FROM sem.v_payments_net WHERE peril = %s",
]


# Each names a type off the allow-list: as ::, CAST, TRY_CAST, a typed literal, an array element, a qualified
# name or a column definition. oid and the reg* family look names up in the catalog.
TYPE_REJECTED = [
    ("SELECT 'pg_authid'::regclass", "regclass"),
    ("SELECT CAST('pg_sleep' AS regproc)", "regproc"),
    ("SELECT TRY_CAST('u_supervisor' AS regrole)", "regrole"),
    ("SELECT regclass 'pg_shadow'", "regclass"),
    ("SELECT 'int4'::regtype", "regtype"),
    ("SELECT claim_id::oid FROM sem.v_claims", "oid"),
    ("SELECT '<x/>'::xml", "xml"),
    ("SELECT '{}'::json", "json"),
    ("SELECT '{}'::jsonb", "jsonb"),
    ("SELECT %s::jsonb", "jsonb"),
    ("SELECT * FROM agg.metric(%s, %s::text[], '{}'::jsonb, %s::date, %s::date)", "jsonb"),
    ("SELECT '{<x/>}'::xml[]", "xml"),
    ("SELECT '{pg_authid}'::_regclass", "_regclass"),
    ("SELECT 'pg_authid'::pg_catalog.regclass", "pg_catalog.regclass"),
    ("SELECT claim_id::name FROM sem.v_claims", "name"),
    ("SELECT * FROM agg.metric(%s) AS t(grp regclass)", "regclass"),
]

TYPE_ALLOWED = [
    "SELECT sum(amount)::numeric, count(*)::bigint FROM sem.v_payments_net",
    "SELECT count(*)::int, count(*)::smallint, CAST(count(*) AS integer) FROM sem.v_claims",
    "SELECT claim_id FROM sem.v_claims WHERE loss_date >= '2025-01-01'::date",
    "SELECT paid_total::real, CAST(paid_total AS double precision), paid_total::numeric(12, 2) FROM sem.v_claims",
    "SELECT claim_id::text, region::varchar(10), state::char(2) FROM sem.v_claims",
    "SELECT (status = 'open')::boolean, loss_date::timestamp, loss_date::timestamptz FROM sem.v_claims",
    "SELECT claim_id FROM sem.v_claims WHERE loss_date > DATE '2025-01-01' - '30 days'::interval",
    "SELECT claim_id FROM sem.v_claims WHERE region = ANY(%s::text[])",
    "SELECT * FROM agg.metric(%s, %s::text[], %s::jsonb, %s::date, %s::date)",
]

RECURSIVE = [
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT n FROM r",
    "WITH RECURSIVE c AS (SELECT claim_id FROM sem.v_claims) SELECT claim_id FROM c",
    "SELECT n FROM (WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT n FROM r LIMIT 5) AS s",
    "WITH a AS (WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT n FROM r) SELECT n FROM a",
]

# No CTE named pg_roles is in scope at the reference, so Postgres looks the name up in the catalog.
OUT_OF_SCOPE = [
    "SELECT r.rolname FROM (WITH pg_roles AS (SELECT 1 AS x) SELECT x FROM pg_roles) AS s CROSS JOIN pg_roles AS r",
    "SELECT rolname FROM pg_roles WHERE EXISTS (WITH pg_roles AS (SELECT 1) SELECT 1 FROM pg_roles)",
    "SELECT (WITH pg_roles AS (SELECT 1 AS n) SELECT n FROM pg_roles) AS n, rolname FROM pg_roles",
    (
        "SELECT x FROM (SELECT rolname AS x FROM pg_roles UNION ALL "
        "(WITH pg_roles AS (SELECT 'a' AS rolname) SELECT rolname FROM pg_roles)) AS s"
    ),
    "WITH a AS (SELECT rolname FROM pg_roles), pg_roles AS (SELECT 1 AS rolname) SELECT rolname FROM a",
    "WITH pg_roles AS (SELECT rolname FROM pg_roles) SELECT rolname FROM pg_roles",
    'WITH "PG_ROLES" AS (SELECT 1 AS rolname) SELECT rolname FROM pg_roles',
    'WITH pg_roles AS (SELECT 1 AS rolname) SELECT rolname FROM "PG_ROLES"',
]

IN_SCOPE = [
    "WITH c AS (SELECT claim_id FROM sem.v_claims) SELECT claim_id FROM c",
    (
        "WITH c AS (SELECT claim_id FROM sem.v_claims) "
        "SELECT count(*) FROM sem.v_claims WHERE claim_id IN (SELECT claim_id FROM c)"
    ),
    (
        "WITH a AS (SELECT claim_id FROM sem.v_claims), "
        "b AS (SELECT claim_id FROM (SELECT claim_id FROM a) AS s) SELECT count(*) FROM b"
    ),
    "SELECT n FROM (WITH c AS (SELECT count(*) AS n FROM sem.v_claims) SELECT n FROM c) AS s",
    (
        "SELECT claim_id FROM (WITH c AS (SELECT claim_id FROM sem.v_claims) "
        "SELECT claim_id FROM c UNION ALL SELECT claim_id FROM c) AS s"
    ),
    'WITH "Recent" AS (SELECT claim_id FROM sem.v_claims) SELECT claim_id FROM "Recent"',
    "WITH Recent AS (SELECT claim_id FROM sem.v_claims) SELECT claim_id FROM RECENT",
]

SHADOWS = [
    ("WITH v_claims AS (SELECT 1 AS claim_id) SELECT claim_id FROM v_claims", "v_claims"),
    ("WITH V_Claims AS (SELECT 1 AS claim_id) SELECT claim_id FROM sem.v_claims", "v_claims"),
    ('WITH "sem.v_claims" AS (SELECT 1 AS claim_id) SELECT claim_id FROM "sem.v_claims"', "sem.v_claims"),
    ("SELECT n FROM (WITH v_premium AS (SELECT 1 AS n) SELECT n FROM v_premium) AS s", "v_premium"),
    ("WITH metric AS (SELECT 1 AS grp) SELECT grp FROM metric", "metric"),
]


# Calls off the allow-list, whether sqlglot parses them as a known function, as an unknown one, or, for user,
# current_role and system_user, as a column.
FUNCTION_REJECTED = [
    ("SELECT version()", "version"),
    ("SELECT current_user", "current_user"),
    ("SELECT session_user", "session_user"),
    ("SELECT current_role", "current_role"),
    ("SELECT user", "user"),
    ("SELECT system_user", "system_user"),
    ("SELECT current_date", "current_date"),
    ("SELECT md5(claim_id::text) FROM sem.v_claims", "md5"),
    ("SELECT string_agg(claim_id::text, ',') FROM sem.v_claims", "string_agg"),
    ("SELECT generate_series(1, 100000000)", "generate_series"),
    ("SELECT xmlelement(NAME claim, claim_id) FROM sem.v_claims", "xmlelement"),
    ("SELECT json_object('claim' VALUE claim_id) FROM sem.v_claims", "json_object"),
    ("SELECT u FROM unnest(ARRAY[1, 2, 3]) AS u", "unnest"),
    ("SELECT PARSE_JSON('{\"x\": 1}') AS j", "parse_json"),
    # Harmless, but nothing the app writes calls them, so they wait until a measure needs one.
    ("SELECT avg(paid_total) FROM sem.v_claims", "avg"),
    ("SELECT min(loss_date) FROM sem.v_claims", "min"),
    ("SELECT max(loss_date) FROM sem.v_claims", "max"),
    ("SELECT round(paid_total, 2) FROM sem.v_claims", "round"),
    ("SELECT extract(year FROM loss_date) FROM sem.v_claims", "extract"),
    ("SELECT date_part('year', loss_date) FROM sem.v_claims", "extract"),
    ("SELECT abs(paid_total) FROM sem.v_claims", "abs"),
    ("SELECT greatest(paid_total, 0) FROM sem.v_claims", "greatest"),
    ("SELECT least(paid_total, 0) FROM sem.v_claims", "least"),
    ("SELECT lower(region) FROM sem.v_claims", "lower"),
    ("SELECT upper(region) FROM sem.v_claims", "upper"),
    ("SELECT to_char(paid_date, 'YYYY-MM') FROM sem.v_payments_net", "to_char"),
]

# The name each call runs under, which for some differs from sqlglot's own name for the node.
PG_NAMED = [
    ("SELECT count(*) FROM sem.v_claims", "count"),
    ("SELECT date_trunc('month', loss_date) FROM sem.v_claims", "date_trunc"),
    ("SELECT to_char(paid_date, 'YYYY-MM') FROM sem.v_payments_net", "to_char"),
    ("SELECT date_part('year', loss_date) FROM sem.v_claims", "extract"),
    ("SELECT string_agg(region, ',') FROM sem.v_claims", "string_agg"),
    ("SELECT version()", "version"),
    ("SELECT generate_series(1, 2)", "generate_series"),
    ("SELECT json_object('k' VALUE 1)", "json_object"),
]

OPERATORS_ONLY = (
    "SELECT CASE WHEN paid_total BETWEEN 1 AND 2 THEN -paid_total ELSE (paid_total + 1) * 2 / 3 - paid_total % 2 END "
    "FROM sem.v_claims WHERE region LIKE 'W%' AND state ILIKE 'c%' AND closed_date IS NULL AND NOT status = 'open' "
    "OR peril IN ('hail', 'wind') OR region = ANY(%s) OR claim_id::text <> '' "
    "OR EXISTS (SELECT 1 FROM sem.v_claims WHERE status = 'open')"
)


def test_hostile_fixture_has_the_expected_count() -> None:
    assert len(HOSTILE) == 92


@pytest.mark.parametrize("statement", HOSTILE)
def test_every_hostile_statement_is_rejected(statement: str) -> None:
    verdict = check(statement)
    assert not verdict.ok
    assert verdict.reason


@pytest.mark.parametrize("statement", ALLOWED)
def test_allowed_statement_passes_with_a_limit(statement: str) -> None:
    verdict = check(statement)
    assert verdict.ok, verdict.reason
    assert "LIMIT 500" in verdict.sql.upper()


def test_a_large_limit_is_clamped_to_the_cap() -> None:
    verdict = check("SELECT claim_id FROM sem.v_claims LIMIT 9999")
    assert verdict.ok
    assert verdict.sql.upper().endswith("LIMIT 500")


def test_a_small_limit_is_left_alone() -> None:
    verdict = check("SELECT claim_id FROM sem.v_claims LIMIT 10")
    assert verdict.ok
    assert verdict.sql.upper().endswith("LIMIT 10")


def test_relation_allow_list_covers_views_and_the_table_function() -> None:
    expected = {"sem.v_claims", "sem.v_payments_net", "sem.v_premium", "sem.v_claim_detail", "agg.metric"}
    assert expected <= ALLOWED_RELATIONS


@pytest.mark.parametrize(("statement", "type_name"), TYPE_REJECTED)
def test_a_type_off_the_allow_list_is_rejected(statement: str, type_name: str) -> None:
    assert check(statement).reason == f"type is not allowed: {type_name}"


def test_a_cast_to_a_type_named_in_a_string_is_rejected() -> None:
    assert check("SELECT CAST('pg_authid', 'regclass')").reason == "a cast must name a type"


@pytest.mark.parametrize("type_name", ["jsonb", "regclass", "oid", "text"])
def test_a_returning_type_the_type_check_cannot_see_is_rejected(type_name: str) -> None:
    verdict = check(f"SELECT JSON_OBJECT('k' VALUE region RETURNING {type_name}) FROM sem.v_claims")
    assert verdict.reason == f"RETURNING type is not allowed: {type_name}"


@pytest.mark.parametrize("statement", TYPE_ALLOWED)
def test_a_cast_to_an_allowed_type_passes(statement: str) -> None:
    verdict = check(statement)
    assert verdict.ok, verdict.reason


@pytest.mark.parametrize("statement", RECURSIVE)
def test_a_recursive_cte_is_rejected(statement: str) -> None:
    assert check(statement).reason == "recursive CTEs are not allowed"


@pytest.mark.parametrize("statement", OUT_OF_SCOPE)
def test_a_name_no_cte_in_scope_covers_must_be_schema_qualified(statement: str) -> None:
    assert check(statement).reason == "relation must be schema-qualified: pg_roles"


def test_an_unquoted_name_folds_only_its_ascii_letters() -> None:
    # The CTE keeps its Kelvin sign in Postgres, so pg_locks below is the catalog view.
    verdict = check("WITH pg_locKs AS (SELECT 1 AS x) SELECT * FROM pg_locks")
    assert verdict.reason == "relation must be schema-qualified: pg_locks"


@pytest.mark.parametrize("statement", IN_SCOPE)
def test_a_reference_to_a_cte_in_scope_passes(statement: str) -> None:
    verdict = check(statement)
    assert verdict.ok, verdict.reason


@pytest.mark.parametrize(("statement", "name"), SHADOWS)
def test_a_cte_never_shadows_an_allowed_relation(statement: str, name: str) -> None:
    assert check(statement).reason == f"CTE name shadows a relation: {name}"


@pytest.mark.parametrize(("statement", "name"), FUNCTION_REJECTED)
def test_a_call_off_the_allow_list_is_rejected(statement: str, name: str) -> None:
    assert check(statement).reason == f"function is not allowed: {name}"


def test_a_table_function_off_the_relation_allow_list_is_rejected() -> None:
    verdict = check("SELECT count(*) FROM generate_series(1, 100000) AS g")
    assert verdict.reason == "relation is not allowed: generate_series"


@pytest.mark.parametrize(
    ("statement", "name"),
    [
        ("SELECT * FROM pg_read_file('/etc/passwd') AS f", "pg_read_file"),
        ("WITH s AS (SELECT pg_sleep(10) AS x) SELECT x FROM s", "pg_sleep"),
    ],
)
def test_a_blocked_function_is_rejected_wherever_it_is_called(statement: str, name: str) -> None:
    assert check(statement).reason == f"function is blocked: {name}"


def test_a_schema_qualified_known_function_is_rejected() -> None:
    verdict = check("SELECT pg_catalog.count(*) FROM sem.v_claims")
    assert verdict.reason == "schema-qualified function calls are not allowed"


def test_the_functions_argument_is_an_allow_list_with_default_deny() -> None:
    assert check("SELECT count(*) FROM sem.v_claims", functions=frozenset()).reason == "function is not allowed: count"
    assert check("SELECT md5(region) FROM sem.v_claims", functions=frozenset({"md5"})).ok


def test_the_sql_that_runs_is_inspected_again() -> None:
    # Allowing parse_json lets the call through, but it runs as a CAST to json, which is caught in the output.
    verdict = check("SELECT PARSE_JSON('{\"x\": 1}') AS j", functions=ALLOWED_FUNCTIONS | {"parse_json"})
    assert verdict.reason == "type is not allowed: json"


@pytest.mark.parametrize(("statement", "name"), PG_NAMED)
def test_a_call_is_named_as_postgres_runs_it(statement: str, name: str) -> None:
    node = sqlglot.parse_one(statement, read="postgres").find(exp.Func)
    assert node is not None
    assert function_name(node) == name
    assert node.sql(dialect="postgres").lower().startswith(f"{name}(")


def test_operators_sqlglot_models_as_functions_are_not_calls() -> None:
    verdict = check(OPERATORS_ONLY)
    assert verdict.ok, verdict.reason


def test_a_cte_never_shadows_an_allowed_view_the_call_does_not_read() -> None:
    verdict = check("WITH v_premium AS (SELECT 1 AS n) SELECT n FROM v_premium", relations=frozenset({"sem.v_claims"}))
    assert verdict.reason == "CTE name shadows a relation: v_premium"
