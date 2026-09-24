from collections.abc import Callable, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.integration

Connection = psycopg.Connection[TupleRow]
Login = Callable[..., Connection]

PROBE_CLAIM = 999_000_001
EMPTY: tuple[Any, ...] = ({}, None, None, None, True)


def metric(
    conn: Connection,
    measure: str,
    group_by: Sequence[str] = (),
    filters: dict[str, Any] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT grp, num, den, n, suppressed FROM agg.metric(%s, %s::text[], %s, %s::date, %s::date)",
        (measure, list(group_by), Jsonb(filters or {}), start, end),
    ).fetchall()


def test_analyst_gets_claim_counts_by_region(login: Login, superuser: Connection) -> None:
    rows = metric(login("u_analyst"), "claim_count", ["region"], start=date(2025, 1, 1), end=date(2025, 12, 31))
    gold = superuser.execute(
        "SELECT region, count(*) FROM core.claims WHERE loss_date BETWEEN '2025-01-01' AND '2025-12-31' GROUP BY 1"
    ).fetchall()
    assert {grp["region"]: (num, n, hidden) for grp, num, _, n, hidden in rows} == {
        region: (Decimal(total), total, False) for region, total in gold
    }


def test_loss_ratio_divides_net_paid_by_earned_premium(login: Login, superuser: Connection) -> None:
    rows = metric(login("u_analyst"), "loss_ratio", ["region"], filters={"year": ["2025"]})
    paid: dict[str, Decimal] = dict(
        superuser.execute(
            "SELECT region, sum(amount) FROM core.payments WHERE status <> 'voided'"
            " AND paid_date BETWEEN '2025-01-01' AND '2025-12-31' GROUP BY 1"
        ).fetchall()
    )
    earned: dict[str, Decimal] = dict(
        superuser.execute(
            "SELECT region, sum(amount) FROM core.earned_premium WHERE month BETWEEN '2025-01-01' AND '2025-12-01'"
            " GROUP BY 1"
        ).fetchall()
    )
    assert {grp["region"]: (num, den) for grp, num, den, _, _ in rows} == {r: (paid[r], earned[r]) for r in paid}


def test_cell_below_the_minimum_count_comes_back_empty(login: Login, superuser: Connection) -> None:
    small = superuser.execute(
        "SELECT state, peril, to_char(loss_date, 'YYYY-MM'), count(*) FROM core.claims"
        " GROUP BY 1, 2, 3 HAVING count(*) BETWEEN 1 AND 9 ORDER BY 4, 1, 2, 3 LIMIT 1"
    ).fetchone()
    large = superuser.execute(
        "SELECT state, peril, to_char(loss_date, 'YYYY-MM'), count(*) FROM core.claims"
        " GROUP BY 1, 2, 3 HAVING count(*) >= 10 ORDER BY 4, 1, 2, 3 LIMIT 1"
    ).fetchone()
    assert small is not None and large is not None
    conn = login("u_analyst")
    for state, peril, month, total in (small, large):
        expected = EMPTY if total < 10 else ({}, Decimal(total), None, total, False)
        assert metric(conn, "claim_count", filters={"state": [state], "peril": [peril], "month": [month]}) == [expected]


def test_cell_one_claim_dominates_comes_back_empty(login: Login, superuser: Connection) -> None:
    cell = superuser.execute(
        "SELECT p.region, c.peril, to_char(p.paid_date, 'YYYY-MM'), sum(p.amount)"
        " FROM core.payments p JOIN core.claims c USING (claim_id) WHERE p.status <> 'voided'"
        " GROUP BY 1, 2, 3 ORDER BY count(DISTINCT p.claim_id) DESC, 1, 2, 3 LIMIT 1"
    ).fetchone()
    assert cell is not None
    region, peril, month, total = cell
    filters = {"region": [region], "peril": [peril], "month": [month]}
    conn = login("u_analyst")
    [(_, num, _, n, hidden)] = metric(conn, "paid_losses", filters=filters)
    assert (num, hidden) == (total, False)
    assert n >= 10

    # The analyst's session cannot see another session's uncommitted rows, so the probe claim is
    # committed for the length of the check and removed after it.
    paid_on = date.fromisoformat(f"{month}-15")
    policy = superuser.execute(
        "SELECT p.policy_id, p.state, a.adjuster_id FROM core.policies p"
        " JOIN core.adjusters a ON a.region = p.region WHERE p.region = %s LIMIT 1",
        (region,),
    ).fetchone()
    assert policy is not None
    try:
        superuser.execute(
            "INSERT INTO core.claims (claim_id, policy_id, region, state, peril, loss_date, reported_date,"
            " closed_date, status, adjuster_id, edition, deductible, damage_estimate, channel)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'closed', %s, 'HO-2025', 1000, %s, 'phone')",
            (PROBE_CLAIM, policy[0], region, policy[1], peril, paid_on - timedelta(days=20),
             paid_on - timedelta(days=15), paid_on, policy[2], total * 2 + 1000),
        )  # fmt: skip
        superuser.execute(
            "INSERT INTO core.payments VALUES (%s, %s, %s, %s, %s, 'indemnity', 'issued')",
            (PROBE_CLAIM, PROBE_CLAIM, region, paid_on, total * 2),
        )
        assert metric(conn, "paid_losses", filters=filters) == [EMPTY]
    finally:
        superuser.execute("DELETE FROM core.payments WHERE claim_id = %s", (PROBE_CLAIM,))
        superuser.execute("DELETE FROM core.claims WHERE claim_id = %s", (PROBE_CLAIM,))


def test_analyst_cannot_lower_the_suppression_threshold(login: Login) -> None:
    conn = login("u_analyst")
    with pytest.raises(errors.UndefinedFunction):
        conn.execute("SELECT * FROM agg.metric('claim_count', min_cell_count => 1)")
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute("SELECT value FROM app.settings")
    conn.autocommit = False
    conn.execute("SET TRANSACTION READ WRITE")
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute("UPDATE app.settings SET value = '1' WHERE key = 'min_cell_count'")
    conn.rollback()


@pytest.mark.parametrize(
    ("measure", "group_by", "filters"),
    [
        ("no_such_measure", [], {}),
        ("claim_count", ["no_such_dimension"], {}),
        ("claim_count", ["region; DROP TABLE core.claims"], {}),
        ("claim_count", ["region", "region"], {}),
        ("claim_count", [], {"no_such_dimension": ["x"]}),
        ("claim_count", [], {"region": "West"}),
        ("loss_ratio", ["peril"], {}),
        ("loss_ratio", [], {"state": ["CO"]}),
    ],
)
def test_unknown_or_unsupported_names_are_rejected(
    login: Login, measure: str, group_by: list[str], filters: dict[str, Any]
) -> None:
    with pytest.raises(errors.InvalidParameterValue):
        metric(login("u_analyst"), measure, group_by, filters)


def test_period_bounds_a_day_apart_cannot_isolate_one_payment(login: Login, superuser: Connection) -> None:
    cell = {"state": ["AZ"], "peril": ["fire"]}
    # The attack this guards: both cumulative cells hold enough claims to publish, and the one day
    # between them holds a single payment.
    precondition = superuser.execute(
        "SELECT count(DISTINCT p.claim_id) FILTER (WHERE p.paid_date <= '2024-06-12'),"
        " count(DISTINCT p.claim_id) FILTER (WHERE p.paid_date <= '2024-06-13'),"
        " count(*) FILTER (WHERE p.paid_date = '2024-06-13')"
        " FROM core.payments p JOIN core.claims c USING (claim_id)"
        " WHERE p.status <> 'voided' AND c.state = 'AZ' AND c.peril = 'fire'"
    ).fetchone()
    assert precondition is not None
    claims_through_12th, claims_through_13th, payments_on_13th = precondition
    assert claims_through_12th >= 10 and claims_through_13th >= 10 and payments_on_13th == 1

    conn = login("u_analyst")
    for end in (date(2024, 6, 12), date(2024, 6, 13)):
        with pytest.raises(errors.InvalidParameterValue, match="last day of a month"):
            metric(conn, "paid_losses", filters=cell, end=end)
    with pytest.raises(errors.InvalidParameterValue, match="first day of a month"):
        metric(conn, "paid_losses", filters=cell, start=date(2024, 6, 13))
    with pytest.raises(errors.InvalidParameterValue, match="last day of a month"):
        metric(conn, "paid_losses", filters=cell, start=date(2024, 2, 1), end=date(2024, 2, 28))


@pytest.mark.parametrize(
    ("start", "end"),
    [(date(2024, 2, 1), date(2024, 2, 29)), (date(2025, 4, 1), date(2025, 6, 30)), (None, date(2024, 12, 31))],
    ids=["leap-february", "quarter", "open-start"],
)
def test_month_aligned_periods_are_answered(login: Login, superuser: Connection, start: date | None, end: date) -> None:
    rows = metric(login("u_analyst"), "claim_count", ["region"], start=start, end=end)
    gold = superuser.execute(
        "SELECT region, count(*) FROM core.claims"
        " WHERE (%(start)s::date IS NULL OR loss_date >= %(start)s) AND loss_date <= %(end)s GROUP BY 1",
        {"start": start, "end": end},
    ).fetchall()
    assert {grp["region"]: (num, n, hidden) for grp, num, _, n, hidden in rows} == {
        region: (Decimal(total), total, False) for region, total in gold
    }


def test_no_time_dimension_is_finer_than_a_month(superuser: Connection) -> None:
    time_dims = superuser.execute("SELECT name FROM agg.allowed_dims WHERE strpos(expr, '%1$I') > 0").fetchall()
    assert {name for (name,) in time_dims} == {"month", "quarter", "year"}


def test_known_gap_complementary_filters_can_still_difference(login: Login, superuser: Connection) -> None:
    # Suppression is judged one result at a time, so a region total minus its other states gives
    # back a state cell that was withheld. Documented in agg.metric; this pins the arithmetic.
    found = superuser.execute(
        "WITH cells AS (SELECT region, state, peril, to_char(loss_date, 'YYYY-MM') AS month, count(*) AS n"
        " FROM core.claims GROUP BY 1, 2, 3, 4)"
        " SELECT s.region, s.state, s.peril, s.month, s.n FROM cells s"
        " WHERE s.n < 10 AND NOT EXISTS (SELECT 1 FROM core.states st WHERE st.region = s.region"
        "  AND st.state <> s.state AND NOT EXISTS (SELECT 1 FROM cells o WHERE o.state = st.state"
        "   AND o.peril = s.peril AND o.month = s.month AND o.n >= 10))"
        " ORDER BY s.n, 1, 2, 3, 4 LIMIT 1"
    ).fetchone()
    assert found is not None
    region, state, peril, month, withheld = found
    others = [
        s for (s,) in superuser.execute("SELECT state FROM core.states WHERE region = %s AND state <> %s", found[:2])
    ]
    conn = login("u_analyst")
    same_cell = {"peril": [peril], "month": [month]}
    assert metric(conn, "claim_count", filters={**same_cell, "state": [state]}) == [EMPTY]
    [(_, region_total, _, _, region_hidden)] = metric(conn, "claim_count", filters={**same_cell, "region": [region]})
    [(_, others_total, _, _, others_hidden)] = metric(conn, "claim_count", filters={**same_cell, "state": others})
    assert not region_hidden and not others_hidden
    assert region_total - others_total == withheld


def test_known_gap_overlapping_month_ranges_can_still_difference(login: Login, superuser: Connection) -> None:
    # The same gap along time: a range through one month minus the range through the month before.
    found = superuser.execute(
        "WITH monthly AS (SELECT state, peril, date_trunc('month', loss_date)::date AS month, count(*) AS n"
        " FROM core.claims GROUP BY 1, 2, 3),"
        " running AS (SELECT *, sum(n) OVER (PARTITION BY state, peril ORDER BY month) AS through FROM monthly)"
        " SELECT state, peril, month, n FROM running WHERE n < 10 AND through - n >= 10"
        " ORDER BY n, 1, 2, 3 LIMIT 1"
    ).fetchone()
    assert found is not None
    state, peril, month, withheld = found
    month_end = (month + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    cell = {"state": [state], "peril": [peril]}
    conn = login("u_analyst")
    assert metric(conn, "claim_count", filters={**cell, "month": [month.strftime("%Y-%m")]}) == [EMPTY]
    [(_, before, _, _, before_hidden)] = metric(conn, "claim_count", filters=cell, end=month - timedelta(days=1))
    [(_, through, _, _, through_hidden)] = metric(conn, "claim_count", filters=cell, end=month_end)
    assert not before_hidden and not through_hidden
    assert through - before == withheld


def test_filter_values_are_matched_as_data(login: Login) -> None:
    assert metric(login("u_analyst"), "claim_count", ["region"], {"region": ["West' OR '1'='1"]}) == []


@pytest.mark.parametrize("role", ["u_adj_west", "u_supervisor"])
def test_only_the_analyst_role_can_call_metric(login: Login, role: str) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        metric(login(role), "claim_count")
