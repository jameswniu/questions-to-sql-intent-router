from datetime import date
from decimal import Decimal

import pytest

from evals.verifier.build import PLAN, build
from evals.verifier.planted import Case, from_line, load, to_line
from evals.verifier.score import report, score
from tests.verify.builders import draft, evidence, hit, paid_claim


def test_the_committed_set_pairs_every_planted_error_with_a_clean_draft() -> None:
    cases = load()
    planted = [case for case in cases if case.expect == "fail"]
    assert len(planted) == len(cases) - len(planted) == 2 * len(PLAN)
    assert {case.mutation for case in planted} == set(PLAN)
    assert all(case.id.startswith(case.question) for case in cases)
    clean = {case.id for case in cases if case.expect == "pass"}
    assert clean == {f"{case.id}-clean" for case in planted} and len({case.id for case in cases}) == len(cases)


def test_the_committed_set_is_caught_without_false_alarms() -> None:
    result = score(load())
    assert result.recall >= 0.9 and result.false_alarms == 0, report(result)


def test_a_case_keeps_its_cells_exact_types_through_the_file() -> None:
    rows = ({"quarter": date(2025, 4, 1), "value": Decimal("11762665.57"), "grp": {"region": "West"}, "hidden": False},)
    case = Case("q-clean", "q", "dana", "clean", (), draft(paid_claim()), evidence(rows, hits=[hit("guide#wind:1")]))
    assert from_line(to_line(case)) == case


@pytest.mark.integration
async def test_the_verifier_catches_errors_planted_in_live_answers() -> None:
    result = score(await build())
    print(f"\n{report(result)}")
    assert result.planted == result.clean == 2 * len(PLAN)
    assert result.recall >= 0.9, report(result)
    assert result.false_alarms == 0, report(result)
