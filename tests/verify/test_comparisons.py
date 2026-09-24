from decimal import Decimal
from typing import Any

import pytest

from app.answer.format import NumberRef, change_refs, ref
from app.answer.types import Claim, Evidence
from app.identity import principal_for
from app.verify import FALLING, HIGHEST, LOWEST, MULTIPLES, RISING, ClaimCheck, verify
from tests.verify.builders import PAID_ROWS, PAID_TEXT, RATE_ROWS, RATE_TEXT, draft, evidence, hit, paid_refs, rate_refs

DANA = principal_for("dana")
# Claims reported fell from 1,000 to 900, written in the rows after paid losses.
COUNT_ROWS: tuple[dict[str, Any], ...] = ({"period": "current", "value": 900}, {"period": "prior", "value": 1000})
BY_REGION: tuple[dict[str, Any], ...] = (
    {"region": "East", "value": 569},
    {"region": "North", "value": 832},
    {"region": "South", "value": 809},
    {"region": "West", "value": 1001},
)


def check(claim: Claim, found: Evidence) -> ClaimCheck:
    (result,) = verify(draft(claim), found, DANA).checks
    return result


def count_refs(offset: int = 2) -> tuple[NumberRef, ...]:
    now, then = (ref(row["value"], "integer", "value", offset + i, "value") for i, row in enumerate(COUNT_ROWS))
    return now, then, *change_refs(now.value, then.value, "integer", (offset, offset + 1))


def changes_only(refs: tuple[NumberRef, ...]) -> tuple[NumberRef, ...]:
    return tuple(r for r in refs if r.row_index is None)


def by_region() -> tuple[NumberRef, ...]:
    return tuple(ref(row["value"], "integer", "value", i, "value") for i, row in enumerate(BY_REGION))


def test_each_word_list_is_its_own() -> None:
    lists = [RISING, FALLING, HIGHEST, LOWEST, tuple(MULTIPLES)]
    assert sum(len(words) for words in lists) == len({word for words in lists for word in words})


@pytest.mark.parametrize(
    ("text", "supported"),
    [
        (PAID_TEXT, True),
        (PAID_TEXT.replace(", up ", ", down "), False),
        ("Paid losses rose $11,422,990 (46.9%) to $35,768,928 in 2025 from $24,345,938 in 2024.", True),
        ("Paid losses fell $11,422,990 (46.9%) to $35,768,928 in 2025 from $24,345,938 in 2024.", False),
        ("Paid losses saw a 46.9% increase, $11,422,990 higher than in 2024.", True),
        ("Paid losses saw a 46.9% decline, $11,422,990 lower than in 2024.", False),
        ("PAID LOSSES WERE UP $11,422,990 IN 2025.", True),
        ("PAID LOSSES WERE DOWN $11,422,990 IN 2025.", False),
    ],
)
def test_a_direction_word_must_match_the_sign_of_the_change_it_goes_with(text: str, supported: bool) -> None:
    assert check(Claim(text, paid_refs(), ()), evidence()).supported is supported


def test_a_flipped_direction_names_the_figure_and_what_the_data_shows() -> None:
    flipped = check(Claim(PAID_TEXT.replace(", up ", ", down "), paid_refs(), ()), evidence())
    assert flipped.reasons == (
        'direction: "down" goes with $11,422,990, which is a rise in the data',
        'direction: "down" goes with 46.9%, which is a rise in the data',
    )
    rate = Claim(RATE_TEXT.replace(", up ", ", down "), rate_refs(), ())
    assert not check(rate, evidence(RATE_ROWS)).supported
    assert check(Claim(RATE_TEXT, rate_refs(), ()), evidence(RATE_ROWS)).supported


def test_the_sign_comes_from_the_rows_not_the_recorded_value() -> None:
    now, then, delta, pct = paid_refs()
    signed_wrong = NumberRef(-delta.value, delta.display, delta.column, None, delta.derivation)
    text = "Paid losses were down $11,422,990 in 2025."
    assert not check(Claim(text, (now, then, signed_wrong, pct), ()), evidence()).supported
    assert check(Claim(text.replace("down", "up"), (now, then, signed_wrong, pct), ()), evidence()).supported


def test_a_direction_resting_on_no_change_is_cut() -> None:
    level_only = check(Claim("Paid losses rose to $35,768,928 in 2025.", paid_refs()[:1], ()), evidence())
    assert level_only.reasons == ('direction: "rose" isn\'t traced to a change in the data',)
    assert not check(Claim("Paid losses rose in 2025.", (), ()), evidence()).supported


def test_a_direction_can_rest_on_a_change_the_claim_traces_without_writing_it() -> None:
    assert check(Claim("Paid losses rose from 2024 to 2025.", changes_only(paid_refs()), ()), evidence()).supported
    fell = check(Claim("Paid losses fell from 2024 to 2025.", changes_only(paid_refs()), ()), evidence())
    assert fell.reasons == ('direction: "fell" goes with the change the claim traces, which is a rise in the data',)


def test_two_directions_are_each_checked_against_their_own_change() -> None:
    found = evidence(PAID_ROWS + COUNT_ROWS)
    refs = paid_refs() + count_refs()
    assert check(Claim("Paid losses rose $11,422,990 while claims fell 100.", refs, ()), found).supported
    assert check(Claim("A 46.9% rise in paid losses came with a 10.0% drop in claims.", refs, ()), found).supported
    swapped = check(Claim("Paid losses fell $11,422,990 while claims rose 100.", refs, ()), found)
    assert swapped.reasons == (
        'direction: "fell" goes with $11,422,990, which is a rise in the data',
        'direction: "rose" goes with 100, which is a fall in the data',
    )
    # With no figure beside it, a word can't be paired with one of two changes that move apart.
    unwritten = changes_only(paid_refs()) + changes_only(count_refs())
    assert not check(Claim("Paid losses and claims both rose.", unwritten, ()), found).supported


def test_a_figure_that_is_a_row_value_is_not_read_as_a_change_of_the_same_size() -> None:
    # Every line of a breakdown carries every group's refs. The East's 900 is also the size of the West's rise, and
    # read as that change it would contradict "down".
    rows: tuple[dict[str, Any], ...] = (
        {"period": "current", "region": "East", "value": 900},
        {"period": "prior", "region": "East", "value": 1000},
        {"period": "current", "region": "West", "value": 1900},
        {"period": "prior", "region": "West", "value": 1000},
    )
    levels = tuple(ref(row["value"], "integer", "value", i, "value") for i, row in enumerate(rows))
    refs = (*levels, *change_refs(900, 1000, "integer", (0, 1)), *change_refs(1900, 1000, "integer", (2, 3)))
    east = "- East: 900 against 1,000, down 100 (10.0%)"
    assert check(Claim(east, refs, ()), evidence(rows)).supported
    assert not check(Claim(east.replace("down", "up"), refs, ()), evidence(rows)).supported


@pytest.mark.parametrize(
    ("text", "supported"),
    [
        ("Claims were highest for West at 1,001 and lowest for East at 569.", True),
        ("Claims were lowest for West at 1,001 and highest for East at 569.", False),
        ("Claims were highest for North at 832.", False),
        ("At 1,001, West had the most claims.", True),
        ("West had the fewest claims, 1,001.", False),
    ],
)
def test_a_superlative_must_go_with_the_largest_or_smallest_value(text: str, supported: bool) -> None:
    assert check(Claim(text, by_region(), ()), evidence(BY_REGION)).supported is supported


def test_a_swapped_superlative_names_each_figure() -> None:
    swapped = check(
        Claim("Claims were lowest for West at 1,001 and highest for East at 569.", by_region(), ()), evidence(BY_REGION)
    )
    assert swapped.reasons == (
        'superlative: "lowest" goes with 1,001, which isn\'t the smallest value of its kind in the evidence',
        'superlative: "highest" goes with 569, which isn\'t the largest value of its kind in the evidence',
    )


def test_a_tie_for_the_top_counts_as_the_top() -> None:
    rows: tuple[dict[str, Any], ...] = ({"region": "East", "value": 700}, {"region": "West", "value": 700})
    refs = tuple(ref(row["value"], "integer", "value", i, "value") for i, row in enumerate(rows))
    assert check(Claim("Claims were highest for East at 700.", refs, ()), evidence(rows)).supported


def test_a_superlative_ranks_rows_of_the_same_period_only() -> None:
    rows: tuple[dict[str, Any], ...] = (
        {"period": "current", "region": "East", "value": 569},
        {"period": "current", "region": "West", "value": 1001},
        {"period": "prior", "region": "East", "value": 1500},
        {"period": "prior", "region": "West", "value": 400},
    )
    refs = tuple(ref(row["value"], "integer", "value", i, "value") for i, row in enumerate(rows))
    text = "Claims were highest for West at 1,001 and lowest for East at 569."
    assert check(Claim(text, refs, ()), evidence(rows)).supported


def test_a_rate_is_ranked_by_the_rate_and_a_withheld_group_is_left_out() -> None:
    # The West has the larger numerator but the smaller rate; the North is withheld, so it has no rate to rank.
    rows: tuple[dict[str, Any], ...] = (
        {"grp": {"region": "East"}, "num": Decimal(30), "den": Decimal(100), "suppressed": False},
        {"grp": {"region": "North"}, "num": None, "den": None, "suppressed": True},
        {"grp": {"region": "West"}, "num": Decimal(40), "den": Decimal(200), "suppressed": False},
    )
    refs = (
        ref(Decimal("0.3"), "percent", "num", 0, "num / den"),
        ref(Decimal("0.2"), "percent", "num", 2, "num / den"),
    )
    assert check(
        Claim("Denials were highest for East at 30.0% and lowest for West at 20.0%.", refs, ()), evidence(rows)
    ).supported
    assert not check(Claim("Denials were highest for West at 20.0%.", refs, ()), evidence(rows)).supported


@pytest.mark.parametrize(
    ("now", "word", "supported"),
    [
        (200, "doubled", True),
        (190, "doubled", True),
        (210, "doubled", True),
        (189, "doubled", False),
        (211, "doubled", False),
        (300, "tripled", True),
        (200, "tripled", False),
        (50, "halved", True),
        (48, "halved", True),
        (47, "halved", False),
        (200, "halved", False),
    ],
)
def test_a_multiple_must_match_the_change_within_its_tolerance(now: int, word: str, supported: bool) -> None:
    rows = ({"period": "current", "value": now}, {"period": "prior", "value": 100})
    refs = change_refs(now, 100, "integer", (0, 1))
    assert check(Claim(f"Claims {word} from 2024 to 2025.", refs, ()), evidence(rows)).supported is supported


def test_a_wrong_multiple_says_what_the_change_comes_to() -> None:
    wrong = check(Claim("Paid losses doubled from 2024 to 2025.", changes_only(paid_refs()), ()), evidence())
    assert wrong.reasons == (
        'multiple: "doubled" goes with the change the claim traces, which comes to 1.47 times the earlier value',
    )
    written = "Paid losses doubled, up $11,422,990 (46.9%) on 2024."
    assert not check(Claim(written, paid_refs(), ()), evidence()).supported


def test_a_multiple_of_nothing_is_not_a_multiple() -> None:
    rows = ({"period": "current", "value": 10}, {"period": "prior", "value": 0})
    refs = change_refs(10, 0, "integer", (0, 1))
    assert not check(Claim("Claims doubled from 2024 to 2025.", refs, ()), evidence(rows)).supported


def test_a_comparative_quoted_from_a_cited_passage_needs_no_ref() -> None:
    storms = hit("memo-25-04#hail:1", body="Hail claims rose sharply after the April storms, the most since 2019.")
    found = evidence(hits=[storms, hit("guide#wind:1")])
    quoted = "Hail claims rose sharply after the April storms, the most since 2019."
    assert check(Claim(quoted, (), ("memo-25-04#hail:1",)), found).supported
    assert not check(Claim(quoted, (), ("guide#wind:1",)), found).supported
    assert not check(Claim(quoted, (), ()), found).supported


def test_the_data_decides_over_a_cited_passage() -> None:
    storms = hit("memo-25-04#hail:1", body="Claims rose after the storms.")
    found = evidence(COUNT_ROWS, hits=[storms])
    claim = Claim("Claims rose 100 after the storms.", count_refs(offset=0), ("memo-25-04#hail:1",))
    assert check(claim, found).reasons == ('direction: "rose" goes with 100, which is a fall in the data',)


@pytest.mark.parametrize(
    ("text", "supported"),
    [
        ("The line items don't add up to the total.", True),
        ("At least one claim was reopened, and at most two.", True),
        ("Hail made up most of the claims.", False),
    ],
)
def test_a_listed_word_in_a_phrase_that_does_not_compare_is_passed_over(text: str, supported: bool) -> None:
    assert check(Claim(text, (), ()), evidence()).supported is supported


def test_names_the_evidence_holds_are_not_comparisons() -> None:
    paid_total = Decimal("16833.71")
    row = {"first_name": "Amanda", "last_name": "Rose", "adjuster_name": "Max Sanchez", "paid_total": paid_total}
    paid = NumberRef(paid_total, "$16,834", "paid_total", 0, "paid_total")
    lookup = "Policyholder Amanda Rose, adjuster Max Sanchez. Paid $16,834."
    assert check(Claim(lookup, (paid,), ()), evidence((row,))).supported
    assert not check(Claim(f"{lookup} Paid losses rose.", (paid,), ()), evidence((row,))).supported
    flag = {"field": "total", "flagged": True, "flag_reason": "does not match payments, line items do not add up"}
    flagged = evidence(({"paid": Decimal("9536.32")},), scan_fields=(flag,))
    record = NumberRef(Decimal("9536.32"), "$9,536.32", "paid", 0, "paid")
    scan = "The total couldn't be read reliably: the line items don't add up to it. The record shows $9,536.32 paid."
    assert check(Claim(scan, (record,), ()), flagged).supported
