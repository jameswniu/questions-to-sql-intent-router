import pytest

from evals.ocr import judge_text, truth
from evals.splits import SPLITS, Split, load

DEGRADED = {"total": "5386.35", "ledger_total": "5386.35", "degraded": "smudge", "mismatch": None}
MISMATCH = {"total": "5936.32", "ledger_total": "9536.32", "degraded": None, "mismatch": "transposed"}
UNREADABLE = (
    "The scanned total on the proof of loss for claim 100357 couldn't be read reliably: the text was too faint or"
    " blurred to read with confidence. The payment record shows $5,386.35 paid on this claim."
)


def test_a_clean_scan_passes_when_it_states_its_true_total() -> None:
    text = "The total on the proof of loss for claim 100357 is $5,386.35, which matches the payment record."
    assert judge_text("answer", text, DEGRADED) is None


def test_a_clean_scan_fails_when_it_flags_a_total_it_could_have_read() -> None:
    assert judge_text("answer", UNREADABLE, DEGRADED) == "flagged a clean scan"


def test_a_degraded_scan_passes_when_it_flags_the_total() -> None:
    assert judge_text("no_wrong_number", UNREADABLE, DEGRADED) is None


def test_a_degraded_scan_fails_when_it_states_another_number_even_beside_a_flag() -> None:
    text = (
        "The total on the proof of loss for claim 100357 is $5,386.85. The payment record shows $5,386.35 paid on"
        " this claim, which doesn't match it."
    )
    assert judge_text("no_wrong_number", text, DEGRADED) == "stated 5386.85"


def test_a_planted_mismatch_passes_only_when_it_is_flagged() -> None:
    flagged = (
        "The total on the invoice for claim 100171 is $5,936.32. The payment record shows $9,536.32 paid on this"
        " claim, which doesn't match it."
    )
    assert judge_text("flag", flagged, MISMATCH) is None
    assert judge_text("flag", "The total on the invoice for claim 100171 is $5,936.32.", MISMATCH) is not None


@pytest.mark.parametrize("split", SPLITS)
def test_each_scan_case_expects_what_its_scan_warrants(split: Split) -> None:
    scans = truth()
    for case in load(split, "ocr"):
        scan = scans[case["scan"]]
        wanted = "flag" if scan["mismatch"] else "no_wrong_number" if scan["degraded"] else "answer"
        assert case["expect"] == wanted, case["id"]
