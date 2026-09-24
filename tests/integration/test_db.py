import uuid
from collections.abc import Callable

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import TupleRow

from app import db
from app.identity import principal_for, principals

pytestmark = pytest.mark.integration

Connection = psycopg.Connection[TupleRow]
Login = Callable[..., Connection]


async def test_run_answers_as_the_user_who_asked() -> None:
    rows = await db.run(principal_for("dana"), "SELECT DISTINCT region FROM sem.v_claims")
    assert (rows.columns, rows.rows, rows.truncated) == (["region"], [("West",)], False)


async def test_each_user_runs_on_a_connection_logged_in_as_their_own_role() -> None:
    for user, role in [("dana", "u_adj_west"), ("omar", "u_adj_east"), ("priya", "u_supervisor"), ("sam", "u_analyst")]:
        rows = await db.run(principal_for(user), "SELECT session_user::text, current_user::text")
        assert rows.rows == [(role, role)]


async def test_row_cap_truncates_and_says_so() -> None:
    rows = await db.run(principal_for("priya"), "SELECT claim_id FROM sem.v_claims ORDER BY claim_id", row_cap=5)
    assert len(rows.rows) == 5
    assert rows.truncated


async def test_slow_query_is_cancelled_and_the_pool_recovers() -> None:
    dana = principal_for("dana")
    with pytest.raises(db.QueryTimeout):
        await db.run(dana, "SELECT count(*) FROM generate_series(1, 10000000000)", timeout_s=0.5)
    assert (await db.run(dana, "SELECT 1")).rows == [(1,)]


async def test_run_refuses_anything_that_is_not_a_query() -> None:
    with pytest.raises(errors.SyntaxError):
        await db.run(principal_for("priya"), "UPDATE core.claims SET reserve = 0")


async def test_writer_logs_requests_but_cannot_read_claims(login: Login, superuser: Connection) -> None:
    request_id = uuid.uuid4()
    await db.write(
        "INSERT INTO ops.request_log (request_id, user_id, role_name, route) VALUES (%s, %s, %s, %s)",
        (request_id, "dana", "u_adj_west", "test"),
    )
    try:
        conn = login("app_writer")
        found = conn.execute("SELECT user_id FROM ops.request_log WHERE request_id = %s", (request_id,)).fetchone()
        assert found == ("dana",)
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT 1 FROM core.claims LIMIT 1")
    finally:
        superuser.execute("DELETE FROM ops.request_log WHERE request_id = %s", (request_id,))


def test_gold_reader_sees_every_region_and_changes_nothing() -> None:
    with db.gold_connection() as conn:
        regions = {row[0] for row in conn.execute("SELECT DISTINCT region FROM core.claims")}
        assert regions == {"North", "South", "East", "West"}
        with pytest.raises(errors.ReadOnlySqlTransaction):
            conn.execute("UPDATE core.claims SET reserve = 0")


SIDE_EFFECTS = [
    "SELECT pg_advisory_lock(1)",
    "SELECT pg_advisory_lock_shared(1, 2)",
    "SELECT pg_try_advisory_lock(1)",
    "SELECT pg_advisory_xact_lock(1)",
    "SELECT pg_try_advisory_xact_lock_shared(1)",
    "SELECT pg_advisory_unlock_all()",
    "SELECT pg_logical_emit_message(false, 'probe', 'x')",
    "SELECT pg_notify('probe', 'x')",
    "SELECT txid_current()",
    "SELECT pg_current_xact_id()",
    "SELECT pg_export_snapshot()",
    "SELECT pg_stat_reset()",
    "SELECT pg_cancel_backend(pg_backend_pid())",
    "SELECT pg_terminate_backend(pg_backend_pid())",
    "SELECT pg_create_physical_replication_slot('probe')",
    "SELECT * FROM ts_stat('SELECT tsv FROM rag.chunks')",
]
# Any advisory lock held by a session of the asking role, which a pooled connection would carry into the next request.
ROLE_ADVISORY_LOCKS = (
    "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid"
    " WHERE l.locktype = 'advisory' AND a.usename = session_user"
)


@pytest.mark.parametrize("statement", SIDE_EFFECTS)
@pytest.mark.parametrize("user", sorted(principals()))
async def test_side_effecting_functions_are_refused_and_the_pooled_connection_stays_clean(
    user: str, statement: str
) -> None:
    principal = principal_for(user)
    with pytest.raises(errors.InsufficientPrivilege, match="permission denied for function"):
        await db.run(principal, statement)
    assert (await db.run(principal, ROLE_ADVISORY_LOCKS)).rows == [(0,)]
