import secrets
import time
from collections.abc import Callable

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.rows import TupleRow

from app.identity import principals

pytestmark = pytest.mark.integration

Connection = psycopg.Connection[TupleRow]
Login = Callable[..., Connection]

ALL_REGIONS = {"North", "South", "East", "West"}
ADJUSTERS = sorted((p.db_role, p.regions[0]) for p in principals().values() if p.kind == "adjuster")
CHAT_ROLES = sorted(p.db_role for p in principals().values())
REGIONAL = ("sem.v_claims", "sem.v_payments_net", "sem.v_premium", "core.claims", "core.payments", "core.policies")


def relation(name: str) -> sql.Identifier:
    return sql.Identifier(*name.split("."))


def regions_seen(conn: Connection, name: str) -> set[str]:
    return {row[0] for row in conn.execute(sql.SQL("SELECT DISTINCT region FROM {}").format(relation(name)))}


def count(conn: Connection, name: str) -> int:
    row = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(relation(name))).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize(("role", "region"), ADJUSTERS)
def test_adjuster_never_sees_another_region(login: Login, superuser: Connection, role: str, region: str) -> None:
    conn = login(role)
    for name in REGIONAL:
        assert regions_seen(conn, name) == {region}, name
    everything = superuser.execute("SELECT count(*) FROM core.claims WHERE region = %s", (region,)).fetchone()
    assert (count(conn, "sem.v_claims"),) == everything


def test_supervisor_sees_every_region(login: Login) -> None:
    conn = login("u_supervisor")
    assert regions_seen(conn, "sem.v_claims") == ALL_REGIONS
    assert regions_seen(conn, "core.payments") == ALL_REGIONS


@pytest.mark.parametrize("name", ["core.claims", "core.policyholders", "sem.v_claims", "sem.v_claim_detail"])
def test_analyst_cannot_read_rows_at_all(login: Login, name: str) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        login("u_analyst").execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(relation(name)))


@pytest.mark.parametrize("column", ["ssn", "dob", "email", "phone"])
def test_adjuster_cannot_read_policyholder_pii(login: Login, column: str) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        login("u_adj_west").execute(sql.SQL("SELECT {} FROM core.policyholders LIMIT 1").format(sql.Identifier(column)))


def test_adjuster_reads_names_only_for_its_own_region(login: Login, superuser: Connection) -> None:
    names = login("u_adj_west").execute("SELECT first_name, last_name, state FROM core.policyholders").fetchall()
    west = {row[0] for row in superuser.execute("SELECT state FROM core.states WHERE region = 'West'")}
    assert names
    assert {state for _, _, state in names} == west


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT set_config('role', 'u_supervisor', false)",
        "SET ROLE chat_supervisor",
        "SET ROLE chat_adjuster",
        "SET ROLE u_supervisor",
        "SET SESSION AUTHORIZATION u_supervisor",
    ],
)
def test_adjuster_cannot_take_on_another_identity(login: Login, statement: str) -> None:
    conn = login("u_adj_west")
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute(statement)
    assert conn.execute("SELECT current_user::text, session_user::text").fetchone() == ("u_adj_west", "u_adj_west")


WRITES = [
    "INSERT INTO core.regions (region) VALUES ('Nowhere')",
    "UPDATE core.claims SET reserve = 0",
    "DELETE FROM core.payments",
    "UPDATE app.settings SET value = '1' WHERE key = 'min_cell_count'",
    "INSERT INTO ops.feedback (request_id, user_id, rating) VALUES (gen_random_uuid(), 'x', 1)",
    "CREATE TABLE public.scratch (x int)",
    "CREATE TEMP TABLE scratch (x int)",
    "SELECT lo_from_bytea(0, 'x')",
]


@pytest.mark.parametrize("statement", WRITES)
@pytest.mark.parametrize("role", CHAT_ROLES)
def test_chat_roles_cannot_write_even_after_asking_for_a_read_write_transaction(
    login: Login, role: str, statement: str
) -> None:
    conn = login(role)
    with pytest.raises((errors.ReadOnlySqlTransaction, errors.InsufficientPrivilege)):
        conn.execute(statement)
    # The read-only default is a convenience the session can turn off; the grants are the boundary.
    conn.autocommit = False
    conn.execute("SET TRANSACTION READ WRITE")
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute(statement)
    conn.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT pg_sleep(0)",
        "SELECT pg_sleep_for('1 second')",
        "SELECT query_to_xml('SELECT 1', true, false, '')",
        "SELECT table_to_xml('core.claims', true, false, '')",
    ],
)
@pytest.mark.parametrize("role", CHAT_ROLES)
def test_chat_roles_cannot_call_stalling_or_query_running_functions(login: Login, role: str, statement: str) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        login(role).execute(statement)


SIDE_EFFECTING_OVERLOADS = (
    "SELECT oid::regprocedure::text, has_function_privilege(%s, oid, 'EXECUTE') FROM pg_proc"
    " WHERE pronamespace = 'pg_catalog'::regnamespace AND ("
    " proname IN ('ts_stat', 'ts_rewrite', 'pg_logical_emit_message', 'pg_notify', 'pg_export_snapshot',"
    "  'txid_current', 'txid_current_if_assigned', 'pg_current_xact_id', 'pg_current_xact_id_if_assigned',"
    "  'pg_cancel_backend', 'pg_terminate_backend')"
    " OR proname ~ '^pg_(try_)?advisory_' OR proname ~ '^pg_stat_reset'"
    " OR proname ~ '(replication_slot|^pg_logical_slot_)')"
)


@pytest.mark.parametrize("role", CHAT_ROLES)
def test_no_overload_of_a_side_effecting_function_is_executable_by_a_chat_role(
    superuser: Connection, role: str
) -> None:
    overloads = superuser.execute(SIDE_EFFECTING_OVERLOADS, (role,)).fetchall()
    assert len(overloads) >= 50  # 21 of them are the advisory lock family alone
    assert [name for name, executable in overloads if executable] == []


def test_role_statement_timeout_cancels_a_runaway_query(login: Login) -> None:
    conn = login("u_adj_west")
    started = time.monotonic()
    with pytest.raises(errors.QueryCanceled):
        conn.execute("SELECT count(*) FROM generate_series(1, 10000000000)")
    assert time.monotonic() - started < 6


def test_definer_view_leaks_other_regions_where_the_invoker_view_does_not(login: Login, superuser: Connection) -> None:
    superuser.execute("DROP SCHEMA IF EXISTS leak_probe CASCADE")
    superuser.execute("CREATE SCHEMA leak_probe")
    try:
        superuser.execute("CREATE VIEW leak_probe.v_claims AS SELECT claim_id, region FROM core.claims")
        superuser.execute("GRANT USAGE ON SCHEMA leak_probe TO chat_adjuster")
        superuser.execute("GRANT SELECT ON leak_probe.v_claims TO chat_adjuster")
        conn = login("u_adj_west")
        assert regions_seen(conn, "leak_probe.v_claims") == ALL_REGIONS
        assert regions_seen(conn, "sem.v_claims") == {"West"}
        conn.close()
    finally:
        superuser.execute("DROP SCHEMA leak_probe CASCADE")


def test_login_missing_from_principals_sees_no_rows(login: Login, superuser: Connection) -> None:
    role, password = "u_probe_unlisted", secrets.token_hex(16)
    superuser.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
    superuser.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
    try:
        superuser.execute(
            sql.SQL("GRANT chat_adjuster TO {} WITH INHERIT TRUE, SET FALSE").format(sql.Identifier(role))
        )
        conn = login(role, password)
        for name in ("sem.v_claims", "core.claims", "core.payments", "core.policyholders", "core.states"):
            assert count(conn, name) == 0, name
        conn.close()
    finally:
        superuser.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_every_row_policy_is_scoped_by_region_or_general(superuser: Connection) -> None:
    policies = superuser.execute(
        "SELECT tablename, policyname, cmd, roles::text[], qual FROM pg_policies WHERE schemaname IN ('core', 'rag')"
    ).fetchall()
    assert policies
    for table, name, cmd, roles, qual in policies:
        assert cmd == "SELECT", name
        if roles == ["agg_owner"]:
            # agg.metric aggregates every row and suppresses small or dominated cells on the way out.
            assert qual == "true", name
            continue
        assert "app.visible_regions()" in qual or "sensitivity = 'general'::text" in qual, (table, name, qual)


def test_every_core_and_rag_table_forces_row_security(superuser: Connection) -> None:
    tables = superuser.execute(
        "SELECT oid::regclass::text, relrowsecurity AND relforcerowsecurity FROM pg_class"
        " WHERE (relnamespace IN ('core'::regnamespace, 'rag'::regnamespace) AND relkind = 'r')"
        " OR oid = 'app.principals'::regclass"
    ).fetchall()
    assert len(tables) == 12
    assert [name for name, forced in tables if not forced] == []


def test_no_view_reads_as_its_owner(superuser: Connection) -> None:
    assert superuser.execute(
        "SELECT count(*) FROM pg_matviews WHERE schemaname IN ('core', 'sem', 'rag', 'agg')"
    ).fetchone() == (0,)
    owner_rights = superuser.execute(
        "SELECT oid::regclass::text FROM pg_class WHERE relkind = 'v'"
        " AND relnamespace IN ('core'::regnamespace, 'sem'::regnamespace, 'rag'::regnamespace, 'agg'::regnamespace)"
        " AND NOT coalesce(reloptions @> '{security_invoker=true}', false)"
    ).fetchall()
    assert owner_rights == []


def test_each_user_maps_to_one_login_role_that_cannot_assume_its_group(superuser: Connection) -> None:
    for p in principals().values():
        memberships = superuser.execute(
            "SELECT r.rolcanlogin, r.rolsuper, r.rolbypassrls, r.rolconnlimit, g.rolname::text,"
            " m.inherit_option, m.set_option"
            " FROM pg_roles r JOIN pg_auth_members m ON m.member = r.oid JOIN pg_roles g ON g.oid = m.roleid"
            " WHERE r.rolname = %s",
            (p.db_role,),
        ).fetchall()
        assert memberships == [(True, False, False, 10, f"chat_{p.kind}", True, False)], p.db_role


def test_payment_region_cannot_disagree_with_its_claim(superuser: Connection) -> None:
    row = superuser.execute("SELECT claim_id FROM core.claims WHERE region = 'West' LIMIT 1").fetchone()
    assert row is not None
    with pytest.raises(errors.ForeignKeyViolation):
        superuser.execute(
            "INSERT INTO core.payments VALUES (-1, %s, 'East', '2025-01-01', 1, 'expense', 'issued')", (row[0],)
        )


def test_chunk_cannot_be_general_when_its_document_is_claim_sensitive(superuser: Connection) -> None:
    row = superuser.execute("SELECT claim_id, region FROM core.claims LIMIT 1").fetchone()
    assert row is not None
    with pytest.raises(errors.ForeignKeyViolation), superuser.transaction():
        superuser.execute(
            "INSERT INTO rag.documents (doc_id, kind, title, region, claim_id, sensitivity, uri, sha256)"
            " VALUES ('probe', 'note', 'probe', %s, %s, 'claim', 'probe', 'probe')",
            (row[1], row[0]),
        )
        superuser.execute(
            "INSERT INTO rag.chunks (chunk_id, doc_id, anchor, section_path, ordinal, body, sensitivity)"
            " VALUES ('probe-1', 'probe', 'probe', 'probe', 1, 'body', 'general')"
        )


PROBE_DOC = "probe-doc"
CHILD_INSERTS = {
    "rag.chunks": "INSERT INTO rag.chunks (chunk_id, doc_id, anchor, section_path, ordinal, body, sensitivity, region,"
    " claim_id) VALUES ('probe-chunk', %s, 'probe', 'probe', 1, 'body', %s, %s, %s)",
    "rag.scan_fields": "INSERT INTO rag.scan_fields (doc_id, field, value, sensitivity, region, claim_id)"
    " VALUES (%s, 'probe-field', 'probe', %s, %s, %s)",
}
# (sensitivity, region, claim) for the document and then for its row; claims are named W1, W2 (West) and E1 (East).
Copy = tuple[str, str, str | None]
MISMATCHES: dict[str, tuple[Copy, Copy]] = {
    # The ingest bug from the review: every other constraint accepts an East document's row on a West claim.
    "east-document-row-on-a-west-claim": (("claim", "East", "E1"), ("claim", "West", "W1")),
    "claim": (("claim", "West", "W1"), ("claim", "West", "W2")),
    "region": (("general", "West", None), ("general", "East", None)),
    "sensitivity": (("general", "West", None), ("claim", "West", "W1")),
}


def resolve(copy: Copy, superuser: Connection) -> tuple[str, str, int | None]:
    sensitivity, region, claim = copy
    if claim is None:
        return sensitivity, region, None
    region_of_claim, nth = {"W": "West", "E": "East"}[claim[0]], int(claim[1:])
    row = superuser.execute(
        "SELECT claim_id FROM core.claims WHERE region = %s ORDER BY claim_id OFFSET %s LIMIT 1",
        (region_of_claim, nth - 1),
    ).fetchone()
    assert row is not None
    return sensitivity, region, row[0]


def add_probe_document(superuser: Connection, copy: tuple[str, str, int | None]) -> None:
    sensitivity, region, claim_id = copy
    superuser.execute(
        "INSERT INTO rag.documents (doc_id, kind, title, region, claim_id, sensitivity, uri, sha256)"
        " VALUES (%s, 'note', 'probe', %s, %s, %s, 'probe', 'probe')",
        (PROBE_DOC, region, claim_id, sensitivity),
    )


@pytest.mark.parametrize("table", list(CHILD_INSERTS))
@pytest.mark.parametrize(("document", "row"), list(MISMATCHES.values()), ids=list(MISMATCHES))
def test_rag_rows_must_carry_their_documents_sensitivity_region_and_claim(
    superuser: Connection, table: str, document: Copy, row: Copy
) -> None:
    doc_copy, row_copy = resolve(document, superuser), resolve(row, superuser)
    with superuser.transaction(force_rollback=True):
        add_probe_document(superuser, doc_copy)
        with pytest.raises(errors.ForeignKeyViolation, match="disagrees with its document"), superuser.transaction():
            superuser.execute(CHILD_INSERTS[table], (PROBE_DOC, *row_copy))
        superuser.execute(CHILD_INSERTS[table], (PROBE_DOC, *doc_copy))
        probe_rows = sql.SQL("SELECT count(*) FROM {} WHERE doc_id = %s").format(relation(table))
        assert superuser.execute(probe_rows, (PROBE_DOC,)).fetchone() == (1,)


@pytest.mark.parametrize("table", list(CHILD_INSERTS))
def test_rag_row_and_its_document_cannot_be_moved_apart(superuser: Connection, table: str) -> None:
    west = resolve(("claim", "West", "W1"), superuser)
    _, _, other_west = resolve(("claim", "West", "W2"), superuser)
    _, _, east = resolve(("claim", "East", "E1"), superuser)
    with superuser.transaction(force_rollback=True):
        add_probe_document(superuser, west)
        superuser.execute(CHILD_INSERTS[table], (PROBE_DOC, *west))
        with pytest.raises(errors.ForeignKeyViolation, match="disagrees with its document"), superuser.transaction():
            move_row = sql.SQL("UPDATE {} SET claim_id = %s WHERE doc_id = %s").format(relation(table))
            superuser.execute(move_row, (other_west, PROBE_DOC))
        with pytest.raises(errors.ForeignKeyViolation, match="disagrees with its chunks"), superuser.transaction():
            superuser.execute(
                "UPDATE rag.documents SET region = 'East', claim_id = %s WHERE doc_id = %s", (east, PROBE_DOC)
            )
