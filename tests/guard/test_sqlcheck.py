import pytest

from app.config import ROOT
from app.sqlcheck import ALLOWED_RELATIONS, check

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
    "SELECT to_char(paid_date, 'YYYY-MM') AS m, sum(amount) FROM sem.v_payments_net GROUP BY 1 ORDER BY 1",
    "SELECT * FROM agg.metric(%s, %s, %s::jsonb, %s, %s)",
    "WITH recent AS (SELECT * FROM sem.v_claims WHERE status = 'open') SELECT count(*) FROM recent",
    "SELECT round(avg(paid_total), 2), count(DISTINCT claim_id) FROM sem.v_claims",
    "SELECT * FROM sem.v_claim_detail WHERE claim_id = %(cid)s",
    "SELECT date_trunc('month', loss_date), extract(year FROM loss_date) FROM sem.v_claims",
    "SELECT coalesce(sum(amount), 0) FROM sem.v_payments_net WHERE peril = %s",
]


def test_hostile_fixture_has_the_expected_count() -> None:
    assert len(HOSTILE) == 59


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
