import json
import re
from pathlib import Path
from typing import Any

import pytest

from tools import recount


def rate(hits: int, n: int) -> dict[str, Any]:
    return {"hits": hits, "n": n, "value": round(hits / n, 4), "low": 0.5, "high": 0.99}


REPORT: dict[str, Any] = {
    "dev": {
        "cases": {"routing": 34},
        "routing": {"accuracy": rate(33, 34), "macro_f1": 0.97, "per_class": {"lookup": {"recall": rate(4, 4)}}},
        "latency": {"why": {"n": 4, "p50_ms": 900, "p95_ms": 1400, "first_event_p50_ms": 2}},
    },
    "shared": {"hostile_sql": {"harmful": 0, "executions": 354, "state_unchanged": True}},
}


def table(name: str) -> str:
    return "{{table " + name + "}}\n"


def number(path: str) -> str:
    return "{{n " + path + "}}"


def repo(root: Path, name: str, template: str, report: dict[str, Any] = REPORT) -> Path:
    """Writes evals/report.json and one docs/templates/<name> file, and returns the template's path."""
    (root / "evals").mkdir(exist_ok=True)
    (root / "evals" / "report.json").write_text(json.dumps(report))
    templates_dir = root / "docs" / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    path = templates_dir / name
    path.write_text(template)
    return path


def test_check_passes_when_there_are_no_templates(tmp_path: Path) -> None:
    assert recount.recount(tmp_path, write=False) == 0


def test_write_renders_a_table_and_a_number_and_check_then_passes(tmp_path: Path) -> None:
    template = f"# Claims\n\n{table('routing')}\nRouting got {number('dev.routing.accuracy')} right.\n"
    repo(tmp_path, "README.md", template)
    assert recount.recount(tmp_path, write=True) == 0
    text = (tmp_path / "README.md").read_text()
    assert "| Accuracy | 0.971 (33 of 34, 95% CI 0.500 to 0.990) | Not run |" in text
    assert "Routing got 33 of 34 right.\n" in text
    assert text.endswith("right.\n")
    assert recount.recount(tmp_path, write=False) == 0


def test_check_fails_when_the_target_was_hand_edited_and_names_the_template(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo(tmp_path, "README.md", table("routing"))
    assert recount.recount(tmp_path, write=True) == 0
    (tmp_path / "README.md").write_text("hand edited\n")
    capsys.readouterr()
    assert recount.recount(tmp_path, write=False) == 1
    out = capsys.readouterr().out
    assert "README.md is out of date. Edit docs/templates/README.md and run make claims" in out


def test_an_unknown_table_name_exits_2(tmp_path: Path) -> None:
    repo(tmp_path, "README.md", table("vibes"))
    assert recount.recount(tmp_path, write=False) == 2


def test_an_unrecognized_placeholder_exits_2(tmp_path: Path) -> None:
    repo(tmp_path, "README.md", "Half a table here: {{table routing}} not alone on its line.\n")
    assert recount.recount(tmp_path, write=False) == 2


def test_a_template_with_no_target_counts_as_out_of_date(tmp_path: Path) -> None:
    repo(tmp_path, "README.md", table("routing"))
    assert recount.recount(tmp_path, write=False) == 1


def test_a_readme_template_goes_to_the_root_and_any_other_goes_to_docs(tmp_path: Path) -> None:
    repo(tmp_path, "README.md", table("routing"))
    (tmp_path / "docs" / "templates" / "schema.md").write_text(table("sql"))
    assert recount.recount(tmp_path, write=True) == 0
    assert (tmp_path / "README.md").exists()
    assert (tmp_path / "docs" / "schema.md").exists()
    assert not (tmp_path / "schema.md").exists()
    assert not (tmp_path / "docs" / "README.md").exists()


@pytest.mark.parametrize("name", sorted(recount.RENDERERS))
def test_every_row_says_how_it_was_made_and_starts_each_cell_with_a_capital(name: str) -> None:
    for report in (REPORT, LIVE_REPORT):
        lines = [line for line in recount.RENDERERS[name](report).splitlines() if line.startswith("|")]
        if not lines:
            continue
        assert lines[1].startswith("|---|")
        for line in [lines[0], *lines[2:]]:
            cells = [cell.strip() for cell in line.strip("|").split(" | ")]
            assert cells[-1], line
            assert all(re.match(r"[A-Z0-9]", cell) for cell in cells), line


def test_the_headline_table_shows_hits_of_n_with_the_interval(tmp_path: Path) -> None:
    table_text = recount.headline(REPORT)
    assert "| Routed to the right path | 33 of 34 (0.50 to 0.99) | Not run |" in table_text


def test_a_number_the_report_does_not_have_says_not_run() -> None:
    assert recount.inline(REPORT, "heldout.routing.accuracy") == "Not run"
    assert recount.inline(REPORT, "dev.routing.macro_f1") == "0.97"


def test_a_route_only_ever_predicted_gets_no_recall_row_and_counts_as_unplaced() -> None:
    report: dict[str, Any] = {
        "dev": {"cases": {"routing": 4}, "routing": {
            "per_class": {
                "lookup": {"recall": rate(3, 4), "support": 4},
                "residue": {"recall": rate(0, 1), "support": 0},
            },
            "confusion": {"lookup": {"lookup": 3, "residue": 1}},
        }},
    }  # fmt: skip
    text = recount.routing(report)
    assert "Recall, Lookup" in text
    assert "Recall, Residue" not in text
    assert "| Unplaced | 1 (n 4) | Not run |" in text


def spread(values: list[Any], hits: list[int] | None = None, n: list[int] | None = None) -> dict[str, Any]:
    """A live metric as evals/live.py writes it."""
    present = [v for v in values if v is not None]
    found: dict[str, Any] = {"values": values, "min": min(present), "max": max(present)}
    if hits is not None and n is not None:
        found |= {"hits": hits, "n": n}
    return found


LIVE_REPORT: dict[str, Any] = {
    **REPORT,
    "live": {
        "date": "2026-09-24",
        "commit": "0123456789abcdef0123456789abcdef01234567-dirty",
        "backend": "gcp.vertex_ai",
        "models": {"fast": "claude-haiku-4-5", "main": "claude-sonnet-5", "check": "gemini-3.8-flash"},
        "runs": 3,
        "splits": ["dev", "heldout"],
        "metrics": {
            "dev": {
                "routing": {
                    "accuracy": spread([0.9706, 0.9118, 1.0], [33, 31, 34], [34, 34, 34]),
                    "macro_f1": spread([0.97, 0.97, 0.97]),
                },
                "abstention": {"wrong_answer": spread([0.05, 0.119, 0.05], [2, 5, 2], [40, 42, 40])},
                "permissions": {"leaks": spread([0, 0, 0])},
                "verifier": {"recall": spread([0.9, 0.95, 0.9])},
            },
            "heldout": {"routing": {"accuracy": spread([1.0, 1.0, 1.0], [26, 26, 26], [26, 26, 26])}},
        },
        "usage": {
            "claude-haiku-4-5": {"calls": 120, "cost_usd": 0.0421},
            "claude-opus-5": {"calls": 3, "cost_usd": None},
            "claude-sonnet-5": {"calls": 40, "cost_usd": 0.0042},
        },
        "cost_usd": 3.4567,
    },
}


def test_the_live_table_says_live_mode_has_not_been_scored_when_the_report_has_no_live_section() -> None:
    assert recount.live(REPORT) == (
        "Live mode hasn't been scored against real models yet. `make eval-live` scores it over three runs.\n"
    )


def test_the_live_table_has_a_row_per_live_metric_as_x_of_n_or_its_spread_over_the_runs() -> None:
    text = recount.live(LIVE_REPORT)
    assert "| Routed to the right path | 31 to 34 of 34 over 3 runs | 26 of 26 |" in text
    assert "| Routing macro F1 | 0.970 | Not run |" in text
    assert "| Wrong answers among all answers | 2 of 40 to 5 of 42 over 3 runs | Not run |" in text
    assert "| Leaks | 0 | Not run |" in text
    # A metric the table has no name for still gets a row, after the named ones.
    assert text.rstrip("\n").endswith("| Verifier, recall | 0.900 to 0.950 over 3 runs | Not run |")
    assert "Scan answers passed" not in text
    about, _, table = text.partition("\n\n")
    assert about.startswith("Scored ") and " runs, with claude-sonnet-5 writing" in about
    assert about.endswith(" in all. Where the runs disagree, a cell shows the range.")
    lines = table.splitlines()
    assert lines[1] == "|---|---|---|"
    for line in [lines[0], *lines[2:]]:
        assert all(re.match(r"[A-Z0-9]", cell.strip()) for cell in line.strip("|").split(" | ")), line


def test_live_numbers_render_for_prose_and_say_not_run_before_a_live_run() -> None:
    rendered = {
        path: recount.inline(LIVE_REPORT, path)
        for path in (
            "live.date",
            "live.runs",
            "live.cost_usd",
            "live.models.main",
            "live.models.check",
            "live.commit",
            "live.usage.claude-haiku-4-5.cost_usd",
            "live.usage.claude-sonnet-5.cost_usd",
            "live.usage.claude-opus-5.cost_usd",
            "live.metrics.dev.routing.accuracy",
            "live.metrics.dev.routing.accuracy.min",
        )
    }
    assert rendered == {
        "live.date": "2026-09-24",
        "live.runs": "3",
        "live.cost_usd": "$3.46",
        "live.models.main": "claude-sonnet-5",
        "live.models.check": "gemini-3.8-flash",
        "live.commit": "0123456-dirty",
        "live.usage.claude-haiku-4-5.cost_usd": "$0.04",
        "live.usage.claude-sonnet-5.cost_usd": "$0.0042",
        "live.usage.claude-opus-5.cost_usd": "Unknown",
        "live.metrics.dev.routing.accuracy": "31 to 34 of 34 over 3 runs",
        "live.metrics.dev.routing.accuracy.min": "0.912",
    }
    assert [recount.inline(REPORT, p) for p in ("live.date", "live.runs", "live.cost_usd")] == ["Not run"] * 3


def test_a_template_renders_the_live_table_and_a_models_numbers(tmp_path: Path) -> None:
    template = f"{table('live')}\nHaiku took {number('live.usage.claude-haiku-4-5.calls')} calls.\n"
    repo(tmp_path, "README.md", template, LIVE_REPORT)
    assert recount.recount(tmp_path, write=True) == 0
    text = (tmp_path / "README.md").read_text()
    assert "| Routed to the right path | 31 to 34 of 34 over 3 runs | 26 of 26 |" in text
    assert "Haiku took 120 calls.\n" in text
    assert recount.recount(tmp_path, write=False) == 0
