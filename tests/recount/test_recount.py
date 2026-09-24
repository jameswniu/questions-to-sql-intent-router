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
    lines = recount.RENDERERS[name](REPORT).splitlines()
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
