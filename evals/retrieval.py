from collections import Counter
from collections.abc import Sequence
from datetime import date
from typing import Any

from app.answer.qual import reading_date
from app.identity import principal_for
from app.sources.documents import Mode, search
from evals.metrics import dedupe, ndcg_at_k, reciprocal_rank
from evals.outcome import rate
from evals.splits import Case

MODES: tuple[Mode, ...] = ("lexical", "vector", "hybrid")
# Hybrid reranks the top 20 fused candidates whatever k is, so asking for 20 keeps the ranking the answer path sees.
DEPTH = 20


async def ranked(case: Case, mode: Mode) -> list[str]:
    """Anchors in rank order, each once, searched as the case user on the date the answer path would read on."""
    principal = principal_for(case["user"])
    on = case.get("on_date")
    day = date.fromisoformat(on) if on else (await reading_date(principal, case["q"])).day
    hits = await search(principal, case["q"], k=DEPTH, as_of_date=day, mode=mode)
    return dedupe(hit.anchor for hit in hits)


def mode_scores(runs: Sequence[tuple[Case, list[str]]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    mrr = ndcg = 0.0
    for case, anchors in runs:
        relevant = set(case["relevant"])
        counts["relevant"] += len(relevant)
        counts["found_5"] += len(relevant & set(anchors[:5]))
        counts["found_10"] += len(relevant & set(anchors[:10]))
        counts["shown_5"] += len(anchors[:5])
        mrr += reciprocal_rank(anchors, relevant, 10)
        ndcg += ndcg_at_k(anchors, relevant, 10)
    n = len(runs)
    return {
        "n": n,
        "recall_at_5": rate(counts["found_5"], counts["relevant"]),
        "recall_at_10": rate(counts["found_10"], counts["relevant"]),
        # Anchors are deduplicated, so the relevant ones among the first five are the ones found there.
        "precision_at_5": rate(counts["found_5"], counts["shown_5"]),
        "mrr_at_10": round(mrr / n, 4) if n else 0.0,
        "ndcg_at_10": round(ndcg / n, 4) if n else 0.0,
    }


async def score_retrieval(cases: Sequence[Case]) -> dict[str, Any]:
    section: dict[str, Any] = {}
    by_kind: dict[str, dict[str, Any]] = {}
    kinds = {case["id"]: principal_for(case["user"]).kind for case in cases}
    for mode in MODES:
        runs = [(case, await ranked(case, mode)) for case in cases]
        section[mode] = mode_scores(runs)
        for kind in sorted(set(kinds.values())):
            mine = [(case, anchors) for case, anchors in runs if kinds[case["id"]] == kind]
            by_kind.setdefault(kind, {})[mode] = mode_scores(mine)["recall_at_5"]
    section["by_kind"] = by_kind
    return section
