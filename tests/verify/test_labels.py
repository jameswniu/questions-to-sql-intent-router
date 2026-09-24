from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.answer.format import NumberRef, change_refs, ref
from app.answer.types import Claim, Evidence
from app.identity import principal_for
from app.verify import COULD_NOT_CONFIRM, ClaimCheck, finalize, verify
from tests.verify.builders import draft, evidence, hit

DANA = principal_for("dana")
BY_REGION: tuple[dict[str, Any], ...] = (
    {"region": "East", "value": 569},
    {"region": "North", "value": 832},
    {"region": "South", "value": 809},
    {"region": "West", "value": 1001},
)
BY_MONTH: tuple[dict[str, Any], ...] = (
    {"month": date(2025, 1, 1), "value": 314},
    {"month": date(2025, 7, 1), "value": 9},
)
# As the aggregate function returns a quarterly split: the group sits in grp, keyed like 2025-Q1.
BY_QUARTER: tuple[dict[str, Any], ...] = (
    {"grp": {"quarter": "2025-Q1"}, "num": Decimal("10106037.54"), "den": None, "n": 900, "suppressed": False},
    {"grp": {"quarter": "2025-Q2"}, "num": Decimal("11762665.57"), "den": None, "n": 950, "suppressed": False},
)


def check(claim: Claim, found: Evidence) -> ClaimCheck:
    (result,) = verify(draft(claim), found, DANA).checks
    return result


def levels(rows: tuple[dict[str, Any], ...], fmt: Any = "integer", column: str = "value") -> tuple[NumberRef, ...]:
    return tuple(ref(row[column], fmt, column, i, column) for i, row in enumerate(rows))


@pytest.mark.parametrize(
    ("text", "supported"),
    [
        ("Claims were highest for West at 1,001 and lowest for East at 569.", True),
        ("Claims were highest for the West at 1,001 and lowest for the East at 569.", True),
        ("- North: 832", True),
        ("- North: 569", False),
        ("Claims in the western region came to 1,001.", True),
        ("Claims in the eastern region came to 1,001.", False),
    ],
)
def test_a_region_named_beside_a_figure_must_be_its_rows_region(text: str, supported: bool) -> None:
    assert check(Claim(text, levels(BY_REGION), ()), evidence(BY_REGION)).supported is supported


def test_a_swapped_region_is_cut_with_the_region_the_row_has() -> None:
    swapped = check(
        Claim("Claims were highest for East at 1,001 and lowest for West at 569.", levels(BY_REGION), ()),
        evidence(BY_REGION),
    )
    assert swapped.reasons == (
        'label: "East" does not match the row 1,001 traces to, which has region West',
        'label: "West" does not match the row 569 traces to, which has region East',
    )
    answer = finalize(draft(swapped.claim), verify(draft(swapped.claim), evidence(BY_REGION), DANA))
    assert answer.could_not_confirm == (COULD_NOT_CONFIRM["label"],)


@pytest.mark.parametrize(
    ("text", "supported"),
    [
        ("Claims ranged from 9 in Jul 2025 to 314 in Jan 2025.", True),
        ("Claims ranged from 9 in July 2025 to 314 in January 2025.", True),
        ("Claims ranged from 9 in Jan 2025 to 314 in Jul 2025.", False),
        ("- Jul 2025: 314", False),
        ("In 2025 claims numbered 9 in one month.", True),
        ("Claims in 2024 came to 9.", False),
    ],
)
def test_a_month_named_beside_a_figure_must_be_its_rows_month(text: str, supported: bool) -> None:
    assert check(Claim(text, levels(BY_MONTH), ()), evidence(BY_MONTH)).supported is supported


def test_a_quarter_is_read_from_the_aggregate_functions_group() -> None:
    refs = levels(BY_QUARTER, "currency", "num")
    assert check(Claim("- Q1 2025: $10,106,038", refs, ()), evidence(BY_QUARTER)).supported
    swapped = check(Claim("- Q2 2025: $10,106,038", refs, ()), evidence(BY_QUARTER))
    assert swapped.reasons == (
        'label: "Q2 2025" does not match the row $10,106,038 traces to, which has quarter Q1 2025',
    )
    # A year holds a quarter inside it.
    assert check(Claim("In 2025 the first quarter paid $10,106,038.", refs, ()), evidence(BY_QUARTER)).supported


def test_a_state_is_named_by_its_name_or_code() -> None:
    rows = ({"state": "TX", "value": 79}, {"state": "CO", "value": 120})
    refs = levels(rows)
    assert check(Claim("Texas had 79 wind claims.", refs, ()), evidence(rows)).supported
    assert check(Claim("Colorado had 79 wind claims.", refs, ()), evidence(rows)).reasons == (
        'label: "Colorado" does not match the row 79 traces to, which has state Texas',
    )
    assert not check(Claim("CO had 79 wind claims.", refs, ()), evidence(rows)).supported


def test_a_group_the_rows_do_not_carry_was_a_filter_and_is_not_checked() -> None:
    rows = ({"value": 79},)
    assert check(Claim("Claims in the East came to 79.", levels(rows), ()), evidence(rows)).supported


def test_a_share_of_a_change_is_checked_against_the_group_rows_it_reads() -> None:
    rows = (
        {"period": "current", "value": Decimal(1000)},
        {"period": "prior", "value": Decimal(400)},
        {"period": "current", "peril": "hail", "value": Decimal(800), "n": 8},
        {"period": "prior", "peril": "hail", "value": Decimal(150), "n": 3},
    )
    share = NumberRef(Decimal(650) / 600, "108.3%", "share", None, "(value[2] - value[3]) / (value[0] - value[1])")
    refs = (share, *change_refs(1000, 400, "currency", (0, 1)))
    assert check(Claim("Hail claims account for 108.3% of the rise.", refs, ()), evidence(rows)).supported
    assert check(Claim("Wind claims account for 108.3% of the rise.", refs, ()), evidence(rows)).reasons == (
        'label: "Wind" does not match the row 108.3% traces to, which has peril hail',
    )


def test_a_name_the_evidence_holds_is_not_a_label() -> None:
    row: dict[str, Any] = {"region": "South", "adjuster_name": "Kevin West", "paid_total": Decimal("16833.71")}
    paid = NumberRef(row["paid_total"], "$16,834", "paid_total", 0, "paid_total")
    assert check(Claim("Kevin West's claim was paid $16,834.", (paid,), ()), evidence((row,))).supported
    unnamed = {"region": "South", "paid_total": Decimal("16833.71")}
    assert not check(Claim("The West was paid $16,834.", (paid,), ()), evidence((unnamed,))).supported


def test_a_figure_the_cited_passage_quotes_is_the_passages() -> None:
    rows = ({"state": "CO", "value": 30},)
    found = evidence(rows, hits=[hit("bulletin#tx:1", body="Texas claims are inspected within 30 days.")])
    text = "Texas claims are inspected within 30 days."
    assert check(Claim(text, levels(rows), ("bulletin#tx:1",)), found).supported
    assert not check(Claim(text, levels(rows), ()), found).supported


def test_a_figure_that_matches_several_rows_holds_when_one_of_them_fits() -> None:
    rows = ({"region": "East", "value": 700}, {"region": "West", "value": 700})
    assert check(Claim("- West: 700", levels(rows), ()), evidence(rows)).supported
    assert not check(Claim("- North: 700", levels(rows), ()), evidence(rows)).supported
