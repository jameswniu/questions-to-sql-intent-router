from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Rate:
    hits: int
    n: int
    low: float
    high: float

    @property
    def value(self) -> float:
        return self.hits / self.n if self.n else 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "hits": self.hits,
            "n": self.n,
            "value": round(self.value, 4),
            "low": round(self.low, 4),
            "high": round(self.high, 4),
        }


def wilson(hits: int, n: int, z: float = 1.96) -> Rate:
    """Wilson score interval. It stays inside [0, 1] and says something honest about 15 of 15."""
    if n == 0:
        return Rate(0, 0, 0.0, 0.0)
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return Rate(hits, n, max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class ClassScores:
    precision: float
    recall: float
    f1: float
    support: int


def confusion(expected: Sequence[str], predicted: Sequence[str]) -> dict[str, dict[str, int]]:
    matrix: dict[str, dict[str, int]] = {}
    for want, got in zip(expected, predicted, strict=True):
        matrix.setdefault(want, {}).setdefault(got, 0)
        matrix[want][got] += 1
    return matrix


def per_class(expected: Sequence[str], predicted: Sequence[str]) -> dict[str, ClassScores]:
    labels = sorted(set(expected) | set(predicted))
    pairs = list(zip(expected, predicted, strict=True))
    scores = {}
    for label in labels:
        tp = sum(1 for w, g in pairs if w == label and g == label)
        fp = sum(1 for w, g in pairs if w != label and g == label)
        fn = sum(1 for w, g in pairs if w == label and g != label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        scores[label] = ClassScores(precision, recall, f1, tp + fn)
    return scores


def macro_f1(scores: dict[str, ClassScores]) -> float:
    present = [s.f1 for s in scores.values() if s.support]
    return sum(present) / len(present) if present else 0.0


def binary(expected: Sequence[bool], predicted: Sequence[bool]) -> ClassScores:
    """Precision, recall and F1 with True as the positive class (for example, should refuse)."""
    pairs = list(zip(expected, predicted, strict=True))
    tp = sum(1 for w, g in pairs if w and g)
    fp = sum(1 for w, g in pairs if not w and g)
    fn = sum(1 for w, g in pairs if w and not g)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return ClassScores(precision, recall, f1, tp + fn)


def recall_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    wanted = set(relevant)
    if not wanted:
        return 0.0
    return len(wanted & set(ranked[:k])) / len(wanted)


def precision_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    top = ranked[:k]
    if not top:
        return 0.0
    wanted = set(relevant)
    return sum(1 for item in top if item in wanted) / len(top)


def reciprocal_rank(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    wanted = set(relevant)
    for position, item in enumerate(ranked[:k], start=1):
        if item in wanted:
            return 1.0 / position
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Binary relevance. A relevant anchor counts once even when two of its chunks are retrieved."""
    wanted = set(relevant)
    seen: set[str] = set()
    dcg = 0.0
    for position, item in enumerate(ranked[:k], start=1):
        if item in wanted and item not in seen:
            dcg += 1.0 / math.log2(position + 1)
            seen.add(item)
    ideal = sum(1.0 / math.log2(position + 1) for position in range(1, min(len(wanted), k) + 1))
    return dcg / ideal if ideal else 0.0


def dedupe(ranked: Iterable[str]) -> list[str]:
    """Several chunks can share an anchor (a split table). Rank by first appearance of each anchor."""
    out: list[str] = []
    for item in ranked:
        if item not in out:
            out.append(item)
    return out


def edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def character_error_rate(pairs: Iterable[tuple[str, str]]) -> float:
    """Total edits over total reference characters, the usual corpus-level CER."""
    edits = chars = 0
    for reference, read in pairs:
        edits += edit_distance(reference, read)
        chars += len(reference)
    return edits / chars if chars else 0.0


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile, so a p95 over 20 samples is an observed value, never an interpolation."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def tally(labels: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(labels).items()))
