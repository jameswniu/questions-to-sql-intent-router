import json
import os
import tempfile
from decimal import Decimal
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


def _plain(value: object) -> float:
    # The live section's model costs are summed as exact Decimals, and the report holds them as JSON numbers.
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"a {type(value).__name__} can't be written into the report")


def dump(report: dict[str, Any]) -> str:
    return json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False, default=_plain) + "\n"


def write(report: dict[str, Any]) -> None:
    """Replaces the report in one step, so a run stopped halfway leaves the old report whole rather than a truncated
    one that the docs and CI read."""
    text = dump(report)
    fd, tmp = tempfile.mkstemp(dir=REPORT.parent, prefix=".report-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, REPORT)
    except BaseException:
        os.unlink(tmp)
        raise


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


def entry(section: Any, *path: str) -> Any:
    """What the report holds at the path, a rate whole with its hits and n, or None."""
    for key in path:
        if not isinstance(section, dict) or key not in section:
            return None
        section = section[key]
    return section


def value(section: Any, *path: str) -> Any:
    found = entry(section, *path)
    return found["value"] if isinstance(found, dict) and "value" in found else found


def headline(section: dict[str, Any], shared: dict[str, Any], *, whole: bool = False) -> dict[str, Any]:
    """The few numbers the dashboard shows per eval run. whole keeps each rate as the report holds it, hits and n
    included, which the live section needs to say x of n."""
    pick = entry if whole else value
    metrics: dict[str, dict[str, Any]] = {
        "routing": {
            "accuracy": pick(section, "routing", "accuracy"),
            "macro_f1": pick(section, "routing", "macro_f1"),
        },
        "refusal": {
            "f1": pick(section, "refusal", "f1"),
            "false_refusal": pick(section, "refusal", "false_refusal"),
            "injections_missed": pick(section, "refusal", "injections_missed"),
        },
        "abstention": {"wrong_answer": pick(section, "abstention", "wrong_answer")},
        "sql": {"execution_accuracy": pick(section, "sql", "execution_accuracy")},
        "retrieval": {f"{m}_recall_at_5": pick(section, "retrieval", m, "recall_at_5") for m in ("lexical", "hybrid")},
        "answers": {
            "grounded": pick(section, "answers", "grounded"),
            "fact_recall": pick(section, "answers", "fact_recall"),
        },
        "why": {"driver_named": pick(section, "why", "driver_named"), "cited": pick(section, "why", "cited")},
        "ocr": {"answers": pick(section, "ocr", "pass"), "total_cer": pick(shared, "ocr_extraction", "total_cer")},
        "permissions": {"leaks": pick(section, "permissions", "leaks")},
        "verifier": {"recall": pick(shared, "verifier", "recall")},
        "hostile_sql": {"harmful": pick(shared, "hostile_sql", "harmful")},
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
