"""Keeps README.md and docs/*.md equal to evals/report.json.

Each published file has a template of the same name in docs/templates/: docs/templates/README.md renders to
the repo root and every other docs/templates/X.md renders to docs/X.md. A template's {{table NAME}} and
{{n PATH}} placeholders pull a table or a single number from the report. python tools/recount.py --write
renders every template; --check exits 1 when a rendered template differs from what is committed.
"""

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
# {{table NAME}} alone on its own line becomes the named Markdown table, trailing newline kept.
TABLE = re.compile(r"^\{\{table ([a-z_]+)\}\}\n", re.MULTILINE)
# {{n PATH}} anywhere in a line becomes one formatted number. PATH is dotted keys, such as dev.routing.accuracy.
NUMBER = re.compile(r"\{\{n ([a-z0-9_.@]+)\}\}")
# Anything else shaped like a placeholder: a typo neither TABLE nor NUMBER matches.
PLACEHOLDER = re.compile(r"\{\{.*?\}\}")
SPLITS = ("dev", "heldout")
MODES = ("lexical", "vector", "hybrid")
Report = dict[str, Any]
Keys = tuple[str, ...]
# A row: its label, the path to its number, the path to its n when the number is not a rate, and how it was made.
Spec = tuple[str, Keys, Keys | None, str]


def get(node: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def cell(value: Any, n: Any = None) -> str:
    """A rate as its value, its hits of n and its 95% Wilson interval. Anything else as itself, with its n."""
    if value is None:
        return "Not run"
    if isinstance(value, dict) and {"hits", "n", "value", "low", "high"} <= value.keys():
        low, high = value["low"], value["high"]
        return f"{value['value']:.3f} ({value['hits']} of {value['n']}, 95% CI {low:.3f} to {high:.3f})"
    if isinstance(value, bool):
        text = "Yes" if value else "No"
    else:
        text = f"{value:.3f}" if isinstance(value, float) else str(value)
    return f"{text} (n {n})" if n is not None else text


def markdown(header: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(text.replace("|", "\\|") for text in row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


def by_split(report: Report, specs: list[Spec], extra: Sequence[tuple[str, ...]] = ()) -> str:
    rows = []
    for label, path, n_path, how in specs:
        cells = [cell(get(report, s, *path), get(report, s, *n_path) if n_path else None) for s in SPLITS]
        rows.append((label, *cells, how))
    return markdown(("Metric", "Dev", "Held-out", "How it was made"), [*rows, *extra])


def single(report: Report, specs: list[Spec]) -> str:
    rows = [(label, cell(get(report, *path), get(report, *n) if n else None), how) for label, path, n, how in specs]
    return markdown(("Metric", "Value", "How it was made"), rows)


def keys(report: Report, *path: str) -> list[str]:
    return sorted({key for s in (*SPLITS, "shared") for key in (get(report, s, *path) or {})})


def title(name: str) -> str:
    return name.replace("_", " ").capitalize()


def routing(report: Report) -> str:
    specs: list[Spec] = [
        ("Accuracy", ("routing", "accuracy"), None, "Routing cases whose route matches the label. The route is the"
         " one in the final Done event, or refuse when the gate refused"),
        ("Macro F1", ("routing", "macro_f1"), ("cases", "routing"), "Mean F1 over the routes in the labels"),
    ]  # fmt: skip
    specs += [
        (
            f"Recall, {title(label)}",
            ("routing", "per_class", label, "recall"),
            None,
            f"Cases labelled {label} routed there",
        )
        for label in keys(report, "routing", "per_class")
        if any(get(report, s, "routing", "per_class", label, "support") for s in SPLITS)
    ]
    unplaced = []
    for s in SPLITS:
        confusion = get(report, s, "routing", "confusion")
        count = None if confusion is None else sum(row.get("residue", 0) for row in confusion.values())
        unplaced.append(cell(count, get(report, s, "cases", "routing")))
    how = "Cases no keyword rule placed. With no key they get the list of what the app answers. Live mode asks a model"
    return by_split(report, specs, [("Unplaced", *unplaced, how)])


def refusal(report: Report) -> str:
    return by_split(report, [
        ("Refusal precision", ("refusal", "precision"), None, "Refused cases that were labelled refuse"),
        ("Refusal recall", ("refusal", "recall"), None, "Cases labelled refuse that were refused"),
        ("Refusal F1", ("refusal", "f1"), ("cases", "routing"), "Refuse is the positive class"),
        ("False refusals", ("refusal", "false_refusal"), None, "Answerable routing cases that were refused"),
        ("Injections missed", ("refusal", "injections_missed"), ("refusal", "injections"),
         "Prompt-injection cases that were not refused, which must be 0"),
        ("Clarify correct", ("abstention", "clarify"), None, "Cases labelled clarify that asked which one"),
        ("Out of data correct", ("abstention", "out_of_data"), None, "Cases labelled out of data that said so"),
        ("Wrong answers", ("abstention", "wrong_answer"), None, "Answers on any scored path that came from the"
         " wrong route or said the wrong thing, over all answers"),
    ])  # fmt: skip


def sql(report: Report) -> str:
    how = ("Rows the pipeline streamed, read as its answer read them, against gold SQL run as gold_reader over the"
           " asker's regions, equal within 0.5% or one cent")  # fmt: skip
    specs: list[Spec] = [("Execution accuracy", ("sql", "execution_accuracy"), None, how)]
    specs += [
        (f"Execution accuracy, {kind}", ("sql", "by_kind", kind), None, f"The same, asked by an {kind} user")
        for kind in keys(report, "sql", "by_kind")
    ]
    return by_split(report, specs)


RETRIEVAL = [
    ("Recall@5", "recall_at_5", "Relevant anchors in the first 5, chunks deduplicated to anchors"),
    ("Recall@10", "recall_at_10", "Relevant anchors in the first 10"),
    ("Precision@5", "precision_at_5", "Share of the first 5 anchors that are relevant"),
    ("MRR@10", "mrr_at_10", "Mean reciprocal rank of the first relevant anchor"),
    ("NDCG@10", "ndcg_at_10", "Binary relevance, each anchor counted once"),
]


def retrieval(report: Report) -> str:
    rows = []
    for split, name in zip(SPLITS, ("dev", "held-out"), strict=True):
        for label, key, how in RETRIEVAL:
            cells = [
                cell(get(report, split, "retrieval", m, key), get(report, split, "retrieval", m, "n")) for m in MODES
            ]
            rows.append((f"{label}, {name}", *cells, f"{how}. Search run as the case user"))
        for kind in keys(report, "retrieval", "by_kind"):
            cells = [cell(get(report, split, "retrieval", "by_kind", kind, m)) for m in MODES]
            rows.append((f"Recall@5, {name}, {kind}", *cells, f"Recall@5 for questions asked by an {kind}"))
    return markdown(("Metric", "Lexical", "Vector", "Hybrid", "How it was made"), rows)


def answers(report: Report) -> str:
    return by_split(report, [
        ("Answered", ("answers", "answered"), None, "Qualitative cases answered with at least one kept sentence"),
        ("Grounded", ("answers", "grounded"), None, "Cases whose kept sentences cite a passage labelled relevant"),
        ("Key facts stated", ("answers", "fact_recall"), None, "Fact values from data/policy.yaml in the answer text"),
        ("Claims cut", ("answers", "claims_cut"), None, "Drafted sentences the verifier cut"),
    ])  # fmt: skip


def why(report: Report) -> str:
    return by_split(report, [
        ("Answered", ("why", "answered"), None, "Why cases answered with at least one kept sentence"),
        ("Driver named", ("why", "driver_named"), None, "The answer names the driver's peril, state and region"),
        ("Cited document", ("why", "cited"), None, "A cited passage comes from the planted event's document"),
    ])  # fmt: skip


def ocr(report: Report) -> str:
    specs: list[Spec] = []
    for split, name in zip(SPLITS, ("dev", "held-out"), strict=True):
        specs.append((f"Scan answers passed, {name}", (split, "ocr", "pass"), None,
                      "Clean scans state the true total, planted mismatches are flagged, degraded scans state"
                      " the true total or flag it and never state another number"))  # fmt: skip
        specs += [
            (f"Scan answers passed, {name}, {title(e)}", (split, "ocr", "by_expect", e), None, f"Cases expecting {e}")
            for e in keys(report, "ocr", "by_expect")
        ]
    specs += [
        ("Character error rate, totals", ("shared", "ocr_extraction", "total_cer"),
         ("shared", "ocr_extraction", "total_chars"), "Edits over characters, stored total against the printed total"),
        ("Flag precision", ("shared", "ocr_extraction", "flag_precision"), None, "Flagged fields that should be"),
        ("Flag recall", ("shared", "ocr_extraction", "flag_recall"), None,
         "Fields read wrong, and totals of planted mismatches, that were flagged"),
    ]  # fmt: skip
    specs += [
        (
            f"Exact match, {title(t)}",
            ("shared", "ocr_extraction", "exact_match", t),
            None,
            "Stored value equals the page",
        )
        for t in keys(report, "ocr_extraction", "exact_match")
    ]
    return single(report, specs)


def permissions(report: Report) -> str:
    runs = ("dev", "permissions", "runs")
    specs: list[Spec] = [
        ("Leaks", ("dev", "permissions", "leaks"), runs, "Forbidden values in any field of any event a browser would"
         " get: other regions' claim numbers, canaries and scan totals, and any policyholder SSN, phone, email or"
         " birth date. Every probe is asked as every user"),
        ("Over-restricted", ("dev", "permissions", "over_restricted"), runs,
         "Supervisor runs refused, not allowed, or told an existing claim was not found"),
        ("Leaks, note search", ("dev", "permissions", "search", "leaks"), ("dev", "permissions", "search", "runs"),
         "Every probe searched straight against the notes as every user, the same values looked for"),
        ("Own notes in answers", ("dev", "permissions", "controls", "own_notes_in_answers"), runs,
         "The control for the leak count: runs that showed the asker a note from their own region. The eval refuses"
         " to report when it is 0"),
        ("Own notes in searches", ("dev", "permissions", "controls", "own_notes_in_searches"),
         ("dev", "permissions", "search", "runs"), "The same control for the note search"),
    ]  # fmt: skip
    specs += [
        (f"Leaks, {title(kind)}", ("dev", "permissions", "by_kind", kind), runs, "Leaks of this kind")
        for kind in keys(report, "permissions", "by_kind")
    ]
    return single(report, specs)


def verifier(report: Report) -> str:
    base = ("shared", "verifier")
    specs: list[Spec] = [
        ("Planted errors caught", (*base, "recall"), None, "An error is caught when every claim carrying it is cut"),
        ("False alarms", (*base, "false_alarms"), None, "Clean drafts with any claim cut"),
        ("Collateral cuts", (*base, "collateral"), (*base, "planted"), "Claims cut beside a planted error"),
    ]
    specs += [
        (f"Caught, {title(m)}", (*base, "by_mutation", m), None, "Planted errors of this kind caught")
        for m in keys(report, "verifier", "by_mutation")
    ]
    return single(report, specs)


def hostile_sql(report: Report) -> str:
    base = ("shared", "hostile_sql")
    return single(report, [
        ("Harmful executions", (*base, "harmful"), (*base, "executions"), "Every hostile statement run as every"
         " chat role. Harmful when it moves the identity or timeout, returns an SSN or a hidden region's value"),
        ("Database unchanged", (*base, "state_unchanged"), None, "Row counts, settings and scratch tables compared"
         " before and after"),
    ])  # fmt: skip


def latency(report: Report) -> str:
    rows = []
    for route in keys(report, "latency"):
        cells = []
        for split in SPLITS:
            t = get(report, split, "latency", route)
            cells.append("Not run" if t is None else f"P50 {t['p50_ms']} ms, p95 {t['p95_ms']} ms, first event"
                         f" {t['first_event_p50_ms']} ms (n {t['n']})")  # fmt: skip
        rows.append((title(route), *cells, "Question to last event, nearest-rank percentiles. Reported, not checked"))
    return markdown(("Route", "Dev", "Held-out", "How it was made"), rows)


def short(value: Any) -> str:
    """A rate as hits of n with its interval to two places, for the one table on the landing page."""
    if isinstance(value, dict) and {"hits", "n", "low", "high"} <= value.keys():
        return f"{value['hits']} of {value['n']} ({value['low']:.2f} to {value['high']:.2f})"
    return cell(value)


def headline(report: Report) -> str:
    rows: list[tuple[str, Keys]] = [
        ("Routed to the right path", ("routing", "accuracy")),
        ("Refused when they should be", ("refusal", "recall")),
        ("Refused only when they should be", ("refusal", "precision")),
        ("Answerable questions refused", ("refusal", "false_refusal")),
        ("Figure answers equal to gold SQL", ("sql", "execution_accuracy")),
        ("Document answers citing a relevant passage", ("answers", "grounded")),
        ("Why answers naming the planted driver", ("why", "driver_named")),
        ("Scan answers passed", ("ocr", "pass")),
        ("Wrong answers among all answers", ("abstention", "wrong_answer")),
    ]
    body = [(label, *(short(get(report, s, *path)) for s in SPLITS)) for label, path in rows]
    return markdown(("Check", "Dev", "Held-out"), body)


def inline(report: Report, path: str) -> str:
    value = get(report, *path.split("."))
    if isinstance(value, dict) and {"hits", "n"} <= value.keys():
        return f"{value['hits']} of {value['n']}"
    if isinstance(value, list):
        return str(len(value))
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return "Not run" if value is None else str(value)


RENDERERS: dict[str, Callable[[Report], str]] = {
    "routing": routing,
    "refusal": refusal,
    "sql": sql,
    "retrieval": retrieval,
    "answers": answers,
    "why": why,
    "ocr": ocr,
    "permissions": permissions,
    "verifier": verifier,
    "hostile_sql": hostile_sql,
    "latency": latency,
    "headline": headline,
}


def templates(root: Path) -> list[Path]:
    return sorted((root / "docs" / "templates").glob("*.md"))


def target(root: Path, template: Path) -> Path:
    return root / template.name if template.name == "README.md" else root / "docs" / template.name


def errors(root: Path, path: Path, text: str) -> list[str]:
    """File and line of every placeholder recount.py cannot render: an unknown table, or neither shape."""
    rel = path.relative_to(root)
    found: list[str] = []
    spans: list[tuple[int, int]] = []
    for m in TABLE.finditer(text):
        spans.append(m.span())
        if m.group(1) not in RENDERERS:
            line = text.count("\n", 0, m.start()) + 1
            found.append(f"{rel}:{line}: unknown table {m.group(1)!r}")
    for m in NUMBER.finditer(text):
        spans.append(m.span())
    for m in PLACEHOLDER.finditer(text):
        if any(start <= m.start() and m.end() <= end for start, end in spans):
            continue
        line = text.count("\n", 0, m.start()) + 1
        found.append(f"{rel}:{line}: unrecognized placeholder {m.group()!r}")
    return found


def render(text: str, report: Report) -> str:
    text = TABLE.sub(lambda m: RENDERERS[m.group(1)](report), text)
    return NUMBER.sub(lambda m: inline(report, m.group(1)), text)


def recount(root: Path, *, write: bool) -> int:
    paths = templates(root)
    if not paths:
        print("no templates")
        return 0
    texts = {path: path.read_text() for path in paths}
    problems = [error for path, text in texts.items() for error in errors(root, path, text)]
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 2
    report = json.loads((root / "evals" / "report.json").read_text())
    if write:
        changed: list[str] = []
        for path, text in texts.items():
            dest = target(root, path)
            fresh = render(text, report)
            if not dest.exists() or dest.read_text() != fresh:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(fresh)
                changed.append(str(dest.relative_to(root)))
        if changed:
            print("wrote " + ", ".join(changed))
        else:
            print("nothing to write")
        return 0
    stale: list[tuple[Path, Path]] = []
    for path, text in texts.items():
        dest = target(root, path)
        if not dest.exists() or dest.read_text() != render(text, report):
            stale.append((path, dest))
    for path, dest in stale:
        print(f"{dest.relative_to(root)} is out of date. Edit {path.relative_to(root)} and run make claims")
    total = len(paths)
    print(f"{total - len(stale)} of {total} files match evals/report.json")
    return 1 if stale else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/recount.py")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="render every template into its target")
    action.add_argument("--check", action="store_true", help="exit 1 when a template differs from its target")
    return recount(ROOT, write=parser.parse_args(argv).write)


if __name__ == "__main__":
    sys.exit(main())
