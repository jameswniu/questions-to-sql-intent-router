from collections.abc import Mapping, Sequence
from typing import Any

from evals.outcome import Outcome, rate

VARIANTS = ("paraphrase", "typo")


def _pair(original: Sequence[bool], variants: Sequence[bool]) -> dict[str, Any]:
    before, after = rate(sum(original), len(original)), rate(sum(variants), len(variants))
    return {"original": before, "variants": after, "drop": round(float(before["value"]) - float(after["value"]), 4)}


def score_robustness(
    variants: Sequence[Outcome],
    routed: Mapping[str, bool],
    executed: Mapping[str, bool],
    variant_executed: Mapping[str, bool],
) -> dict[str, Any]:
    """The paraphrase and typo set against the originals it was written from. routed and executed hold each
    original's result by case id; only originals that have variants are counted, so both sides cover the same
    questions."""
    covered = sorted({o.case["of"] for o in variants})
    right = {o.case["id"]: o.label == o.case["route"] for o in variants}
    by_variant = {}
    for kind in VARIANTS:
        mine = [o for o in variants if o.case["variant"] == kind]
        by_variant[kind] = {
            "routing": rate(sum(right[o.case["id"]] for o in mine), len(mine)),
            "sql": rate(
                sum(variant_executed[o.case["id"]] for o in mine if o.case["id"] in variant_executed),
                sum(1 for o in mine if o.case["id"] in variant_executed),
            ),
        }
    return {
        "routing": _pair([routed[i] for i in covered if i in routed], list(right.values())),
        "sql": _pair([executed[i] for i in covered if i in executed], list(variant_executed.values())),
        "by_variant": by_variant,
        "misrouted": sorted(case_id for case_id, ok in right.items() if not ok),
    }
