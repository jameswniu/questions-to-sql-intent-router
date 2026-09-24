import json
from fnmatch import fnmatch
from typing import Any, NamedTuple

from app.config import ROOT

REPORT = ROOT / "evals" / "report.json"
TOLERANCE = 0.02
# Keys that move with the embedding model and the reranker, whose floats can differ from one CPU to another. Their
# rates are compared by value; their counts are not compared.
TOLERANT = (
    "*.retrieval.vector.*",
    "*.retrieval.hybrid.*",
    "*.retrieval.by_kind.*.vector.*",
    "*.retrieval.by_kind.*.hybrid.*",
    "*.answers.*",
    "*.why.cited.*",
    "*.abstention.wrong_answer.*",
    "*.permissions.controls.*",
)
# Timing is reported and never compared.
IGNORED = ("*.latency.*",)
MISSING = "(missing)"


class Difference(NamedTuple):
    key: str
    committed: Any
    fresh: Any
    rule: str


def read() -> dict[str, Any]:
    return json.loads(REPORT.read_text()) if REPORT.exists() else {}


def dump(report: dict[str, Any]) -> str:
    return json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        return {prefix: value}
    flat: dict[str, Any] = {}
    for key, child in value.items():
        flat.update(flatten(child, f"{prefix}.{key}" if prefix else str(key)))
    return flat


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def compare(committed: dict[str, Any], fresh: dict[str, Any], sections: list[str]) -> list[Difference]:
    old = {k: v for k, v in flatten(committed).items() if k.split(".", 1)[0] in sections}
    new = {k: v for k, v in flatten(fresh).items() if k.split(".", 1)[0] in sections}
    found = []
    for key in sorted(old.keys() | new.keys()):
        if any(fnmatch(key, pattern) for pattern in IGNORED):
            continue
        before, after = old.get(key, MISSING), new.get(key, MISSING)
        if any(fnmatch(key, pattern) for pattern in TOLERANT):
            if isinstance(before, int) and isinstance(after, int) and not isinstance(before, bool):
                continue
            if key.rsplit(".", 1)[-1] in ("low", "high"):
                continue
            if _number(before) and _number(after):
                if abs(float(before) - float(after)) > TOLERANCE:
                    found.append(Difference(key, before, after, f"within {TOLERANCE}"))
                continue
        if before != after:
            found.append(Difference(key, before, after, "exact"))
    return found


def table(differences: list[Difference], limit: int = 40) -> str:
    rows = [("Key", "Committed", "Fresh", "Rule")]
    rows += [(d.key, _short(d.committed), _short(d.fresh), d.rule) for d in differences[:limit]]
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    lines = ["  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)) for row in rows]
    if len(differences) > limit:
        lines.append(f"... and {len(differences) - limit} more")
    return "\n".join(lines)


def _short(value: Any) -> str:
    text = json.dumps(value) if not isinstance(value, str) else value
    return text if len(text) <= 48 else text[:45] + "..."


def value(section: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(section, dict) or key not in section:
            return None
        section = section[key]
    return section["value"] if isinstance(section, dict) and "value" in section else section


def headline(section: dict[str, Any], shared: dict[str, Any]) -> dict[str, Any]:
    """The few numbers the dashboard shows per eval run."""
    metrics: dict[str, dict[str, Any]] = {
        "routing": {
            "accuracy": value(section, "routing", "accuracy"),
            "macro_f1": value(section, "routing", "macro_f1"),
        },
        "refusal": {
            "f1": value(section, "refusal", "f1"),
            "false_refusal": value(section, "refusal", "false_refusal"),
            "injections_missed": value(section, "refusal", "injections_missed"),
        },
        "abstention": {"wrong_answer": value(section, "abstention", "wrong_answer")},
        "sql": {"execution_accuracy": value(section, "sql", "execution_accuracy")},
        "retrieval": {f"{m}_recall_at_5": value(section, "retrieval", m, "recall_at_5") for m in ("lexical", "hybrid")},
        "answers": {
            "grounded": value(section, "answers", "grounded"),
            "fact_recall": value(section, "answers", "fact_recall"),
        },
        "why": {"driver_named": value(section, "why", "driver_named"), "cited": value(section, "why", "cited")},
        "ocr": {"answers": value(section, "ocr", "pass"), "total_cer": value(shared, "ocr_extraction", "total_cer")},
        "permissions": {"leaks": value(section, "permissions", "leaks")},
        "verifier": {"recall": value(shared, "verifier", "recall")},
        "hostile_sql": {"harmful": value(shared, "hostile_sql", "harmful")},
    }
    kept = {group: {k: v for k, v in numbers.items() if v is not None} for group, numbers in metrics.items()}
    return {group: numbers for group, numbers in kept.items() if numbers}


def summary(fresh: dict[str, Any]) -> str:
    splits = [split for split in ("dev", "heldout") if split in fresh]
    lines = []
    for split in splits:
        flat = flatten(headline(fresh[split], fresh.get("shared", {})))
        lines.append(f"{split}: " + ", ".join(f"{key} {number}" for key, number in sorted(flat.items())))
    return "\n".join(lines)
