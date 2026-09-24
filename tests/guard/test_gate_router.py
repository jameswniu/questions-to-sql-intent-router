import json
from collections import Counter

from app.config import ROOT
from app.gate import screen
from app.route import route

_LINES = (ROOT / "evals" / "cases" / "routing.jsonl").read_text().splitlines()
_CASES = [json.loads(line) for line in _LINES if line.strip()]
_REFUSALS = [c for c in _CASES if c["expect"] == "refuse"]
_BENIGN = [c for c in _CASES if c["expect"] != "refuse"]


def test_gate_refuses_each_expected_refusal_with_the_right_reason() -> None:
    for case in _REFUSALS:
        refusal = screen(case["q"])
        assert refusal is not None, case["id"]
        assert refusal.reason == case["refusal_reason"], (case["id"], refusal.reason)


def test_gate_passes_every_benign_question() -> None:
    refused = [c["id"] for c in _BENIGN if screen(c["q"]) is not None]
    assert refused == []


def test_router_matches_every_benign_dev_case() -> None:
    wrong = [(c["id"], c["route"], route(c["q"]).route) for c in _BENIGN if route(c["q"]).route != c["route"]]
    assert wrong == []


def test_benign_trigger_words_are_not_refused() -> None:
    for cid in ("route-032", "route-033", "route-034"):
        case = next(c for c in _CASES if c["id"] == cid)
        assert screen(case["q"]) is None
        assert route(case["q"]).route == case["route"]


def test_metrics_report() -> None:
    total: Counter[str] = Counter()
    correct: Counter[str] = Counter()
    for case in _BENIGN:
        total[case["route"]] += 1
        if route(case["q"]).route == case["route"]:
            correct[case["route"]] += 1

    tp = fp = fn = tn = 0
    for case in _CASES:
        refused = screen(case["q"]) is not None
        want = case["expect"] == "refuse"
        if refused and want:
            tp += 1
        elif refused and not want:
            fp += 1
        elif not refused and want:
            fn += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    false_refusal = fp / (fp + tn) if fp + tn else 0.0

    print("\nrouting accuracy by class (dev):")
    for cls in sorted(total):
        print(f"  {cls:12s} {correct[cls]}/{total[cls]}")
    print(f"refusal precision {precision:.2f}  recall {recall:.2f}  false-refusal-rate {false_refusal:.2f}")

    assert precision == 1.0
    assert recall == 1.0
    assert false_refusal == 0.0
    assert sum(correct.values()) == sum(total.values())
