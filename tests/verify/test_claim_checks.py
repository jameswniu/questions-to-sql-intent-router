from decimal import Decimal

from app.answer.format import NumberRef
from app.answer.types import Claim, Evidence
from app.identity import principal_for
from app.verify import ClaimCheck, SecondCheck, verify
from tests.verify.builders import (
    PAID_ROWS,
    PAID_TEXT,
    RATE_ROWS,
    RATE_TEXT,
    draft,
    evidence,
    hit,
    paid_claim,
    paid_refs,
    rate_refs,
)

DANA = principal_for("dana")  # an adjuster who sees the West only


def check(claim: Claim, found: Evidence, second_check: SecondCheck | None = None) -> ClaimCheck:
    (result,) = verify(draft(claim), found, DANA, second_check=second_check).checks
    return result


def test_figures_that_trace_to_the_rows_pass() -> None:
    both = draft(paid_claim(), Claim(RATE_TEXT, rate_refs(rows_offset=2), ()))
    verification = verify(both, evidence(PAID_ROWS + RATE_ROWS), DANA)
    assert verification.passed, [c.reasons for c in verification.checks]


def test_a_figure_no_ref_matches_is_cut_and_the_closest_ref_named() -> None:
    result = check(paid_claim(PAID_TEXT.replace("$35,768,928", "$35,768,982")), evidence())
    assert not result.supported
    assert result.reasons == ("figure: $35,768,982 matches none of the claim's numbers (closest is $35,768,928)",)


def test_a_ref_whose_recorded_derivation_does_not_hold_is_cut() -> None:
    now, then, delta, pct = paid_refs()
    wrong = NumberRef(delta.value / now.value, "31.9%", pct.column, None, pct.derivation)
    claim = Claim(PAID_TEXT.replace("46.9%", "31.9%"), (now, then, delta, wrong), ())
    (reason,) = check(claim, evidence()).reasons
    assert reason.startswith("figure: 31.9% cites 31.9%, recorded as (value[0] - value[1]) / value[1]")


def test_a_value_from_another_row_does_not_trace_even_though_the_evidence_holds_it() -> None:
    prior = PAID_ROWS[1]["value"]
    claim = Claim("Paid losses were $24,345,938 in 2025.", (NumberRef(prior, "$24,345,938", "value", 0, "value"),), ())
    assert not check(claim, evidence()).supported


def test_sandbox_outputs_and_scan_fields_ground_figures_at_any_depth() -> None:
    found = evidence(
        rows=(),
        sandbox=({"slope": 12345.6, "rows": [{"period": "2025-03", "z": 2.41}]},),
        scan_fields=({"field": "total", "value": "$17,333.71", "confidence": 0.93},),
    )
    refs = (
        NumberRef(Decimal("12345.6"), "$12,346", "slope", None, "slope"),
        NumberRef(Decimal("2.41"), "2.41", "z", None, "rows[0].z"),
        NumberRef(Decimal("17333.71"), "$17,333.71", "total", None, "total"),
    )
    text = "The trend adds $12,346 a month, March sat 2.41 deviations out, and the invoice totals $17,333.71."
    assert check(Claim(text, refs, ()), found).supported


def test_a_value_two_evidence_figures_imply_is_grounded() -> None:
    found = evidence(
        rows=({"claim_id": 100013, "paid_total": Decimal("16833.71")},),
        scan_fields=({"field": "total", "value": "17333.71"},),
    )
    gap = NumberRef(Decimal("500.00"), "$500.00", "total", None, "total - paid_total")
    assert check(Claim("The invoice runs $500.00 above the ledger.", (gap,), ()), found).supported


def test_a_figure_resting_on_nothing_in_the_evidence_is_cut() -> None:
    made_up = NumberRef(Decimal(2750), "$2,750", "value", None, "value")
    (reason,) = check(Claim("Reopened claims made up $2,750 of that.", (made_up,), ()), evidence()).reasons
    assert reason == "figure: $2,750 cites $2,750, which isn't in the evidence and doesn't follow from it"


def test_a_derivation_must_rest_on_a_cell_of_the_evidence() -> None:
    text = "Reopened claims were paid $2,750."
    constant = NumberRef(Decimal(2750), "$2,750", "value", 0, "2750")
    assert check(Claim(text, (constant,), ()), evidence()).reasons == (
        "figure: $2,750 cites $2,750, recorded as 2750, which names no cell of the evidence",
    )
    padded = NumberRef(Decimal(2750), "$2,750", "value", 0, "value[0] * 0 + 2750")
    assert check(Claim(text, (padded,), ()), evidence()).reasons == (
        "figure: $2,750 cites $2,750, recorded as value[0] * 0 + 2750, which leans on the literal 0 rather than "
        "the evidence",
    )


def test_a_percent_may_scale_a_share_of_the_rows_by_100_and_nothing_else() -> None:
    points = Decimal(97) / Decimal(516) * 100
    for derivation in ("numerator[0] / denominator[0] * 100", "100 * numerator[0] / denominator[0]"):
        rate = NumberRef(points, "18.8%", "value", 0, derivation)
        assert check(Claim("The denial rate was 18.8% in 2025.", (rate,), ()), evidence(RATE_ROWS)).supported
    shifted = NumberRef(points + 100, "118.8%", "value", 0, "numerator[0] / denominator[0] * 100 + 100")
    assert not check(Claim("The denial rate was 118.8% in 2025.", (shifted,), ()), evidence(RATE_ROWS)).supported


def test_identifier_columns_do_not_ground_a_figure() -> None:
    found = evidence(rows=({"claim_id": 100013, "claim_number": "100013", "paid_total": Decimal(10)},))
    ref = NumberRef(Decimal(100013), "$100,013", "claim_id", None, "claim_id")
    assert not check(Claim("Paid $100,013.", (ref,), ()), found).supported


def test_a_ref_without_a_recorded_derivation_is_cut() -> None:
    now = paid_refs()[0]
    claim = Claim("Paid losses were $35,768,928 in 2025.", (NumberRef(now.value, now.display, "value", 0, " "),), ())
    assert not check(claim, evidence()).supported


def test_labels_in_a_claim_need_no_ref() -> None:
    claim = Claim("Claim 100013 on the HO-2025 form was reported June 30, 2026, in Q2 2026 [1].", (), ())
    assert check(claim, evidence()).supported


def test_a_citation_must_name_a_chunk_retrieved_for_this_question() -> None:
    result = check(paid_claim(citations=("memo-25-01#rates:1",)), evidence(hits=[hit("guide#wind:1")]))
    assert result.reasons == ("source: memo-25-01#rates:1 wasn't retrieved for this question",)


def test_a_retrieved_chunk_outside_the_askers_regions_is_cut() -> None:
    hits = [hit("note-1#east:1", region="East"), hit("note-2#west:1", region="West"), hit("guide#wind:1")]
    assert not check(paid_claim(citations=("note-1#east:1",)), evidence(hits=hits)).supported
    kept = check(paid_claim(citations=("note-2#west:1", "guide#wind:1")), evidence(hits=hits))
    assert kept.supported and [h.chunk_id for h in kept.sources] == ["note-2#west:1", "guide#wind:1"]


def test_the_second_check_reads_only_cited_claims_that_already_passed() -> None:
    read: list[tuple[str, str]] = []

    def reader(claim: Claim, cited: str) -> bool:
        read.append((claim.text, cited))
        return "deadline" in claim.text

    found = evidence(hits=[hit("guide#wind:1", body="Inspect within 10 days.")])
    claims = (
        Claim("Wind claims wait a month for inspection.", (), ("guide#wind:1",)),
        Claim("The guide sets an inspection deadline.", (), ("guide#wind:1",)),
        paid_claim(),
        Claim("Paid losses were $1 in 2025.", (), ("guide#wind:1",)),
    )
    checks = verify(draft(*claims), found, DANA, second_check=reader).checks
    cited = "Handling\nInspect within 10 days."
    assert read == [(claims[0].text, cited), (claims[1].text, cited)]
    assert checks[0].reasons == ("reading: the cited text doesn't bear the claim out",)
    assert [c.supported for c in checks] == [False, True, True, False]


def test_comparative_wording_resting_on_nothing_is_cut() -> None:
    result = check(Claim("Wind denials doubled, and most of them were in the West.", (), ()), evidence())
    assert result.reasons == (
        'multiple: "doubled" isn\'t traced to a change in the data',
        'superlative: "most" isn\'t traced to a figure in the data',
    )


def test_a_draft_with_no_claims_does_not_pass() -> None:
    assert not verify(draft(), evidence(), DANA).passed


def test_a_figure_quoted_from_a_cited_passage_needs_no_ref() -> None:
    passage = "Wind claims are inspected within 30 days, and the deductible is 2% of Coverage A."
    found = evidence(hits=[hit("guide#wind:1", body=passage), hit("guide#hail:1", body="Hail within 45 days.")])
    assert check(Claim(passage, (), ("guide#wind:1",)), found).supported
    misquoted = passage.replace("30 days", "45 days")
    assert not check(Claim(misquoted, (), ("guide#wind:1",)), found).supported
    assert not check(Claim(passage, (), ()), found).supported
