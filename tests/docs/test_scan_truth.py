from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.seed import scans
from app.seed.notes import ClaimFacts


def test_regenerating_reproduces_the_committed_truth_exactly(tmp_path: Path) -> None:
    scans.build(tmp_path, images=False)
    assert (tmp_path / "truth.jsonl").read_bytes() == scans.TRUTH_PATH.read_bytes()


def test_every_truth_row_has_its_image_and_nothing_else_is_there(truth: list[dict[str, Any]]) -> None:
    assert {row["file"] for row in truth} == {path.name for path in scans.SCANS_DIR.glob("*.png")}


def test_scans_spread_evenly_over_kinds_and_regions(truth: list[dict[str, Any]]) -> None:
    assert len(truth) == 60
    assert Counter(row["kind"] for row in truth) == {"invoice": 20, "estimate": 20, "proof_of_loss": 20}
    assert set(Counter(row["region"] for row in truth).values()) == {15}
    assert len({row["claim_id"] for row in truth}) == 60


def test_planted_mismatches_and_degradations_are_separate_scans(truth: list[dict[str, Any]]) -> None:
    mismatched = {row["doc_id"] for row in truth if row["mismatch"]}
    degraded = {row["doc_id"] for row in truth if row["degraded"]}
    assert len(mismatched) == 8 and len(degraded) == 8
    assert not mismatched & degraded


def test_totals_match_paid_indemnity_except_where_planted(
    truth: list[dict[str, Any]], claim_facts: dict[int, ClaimFacts]
) -> None:
    for row in truth:
        paid = claim_facts[row["claim_id"]].paid_indemnity()
        assert Decimal(row["ledger_total"]) == paid
        items = sum((Decimal(item["amount"]) for item in row["items"]), Decimal(0))
        assert not row["items"] or items == paid, row["doc_id"]
        printed = row["total"]
        if row["mismatch"] is None:
            assert Decimal(printed) == paid, row["doc_id"]
        elif row["mismatch"] == "transposed":
            assert Decimal(printed) != paid and sorted(printed) == sorted(f"{paid:.2f}"), row["doc_id"]
        else:
            assert printed == f"{paid:.2f}".replace(".", ""), row["doc_id"]
