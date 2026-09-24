from typing import Any

import pandas as pd
import pytest

from app.answer import drivers as dv
from app.answer.why import answer_why
from app.identity import principal_for
from app.sandbox import templates
from app.sandbox.client import Table
from app.semantic.describe import state_names
from app.verify import verify
from tests.answer.conftest import dev_cases
from tests.docs.requires import needs_models

pytestmark = pytest.mark.integration

WHY = dev_cases("why.jsonl")


def driver_words(driver: dict[str, Any]) -> list[str]:
    words = [driver.get("region"), driver.get("peril"), *driver.get("regions", [])]
    words.append(state_names()[driver["state"]] if "state" in driver else None)
    return [word for word in words if word]


async def decompose_here(role: str, table: Table, params: dict[str, Any]) -> dict[str, Any]:
    return templates.decompose(pd.DataFrame(table.rows, columns=table.columns), params)


@needs_models
@pytest.mark.parametrize("case", WHY, ids=[case["id"] for case in WHY])
async def test_a_dev_why_question_names_its_planted_driver_and_cites_its_document(case: dict[str, Any]) -> None:
    principal = principal_for(case["user"])
    result = await answer_why(principal, case["q"])
    assert result.kind == "answer"
    for word in driver_words(case["driver"]):
        assert word.lower() in result.text.lower(), f"{word} is not named"
    cited = {c for claim in result.draft.claims for c in claim.citations}
    assert {hit.doc_id for hit in result.evidence.hits if hit.chunk_id in cited} == set(case["cite"])
    assert result.evidence.sandbox, "the change was not decomposed"
    checks = verify(result.draft, result.evidence, principal).checks
    assert all(check.supported for check in checks), [check.reasons for check in checks if not check.supported]


@needs_models
async def test_the_sandbox_and_the_template_split_the_change_alike() -> None:
    dana = principal_for("dana")
    question = "Why were paid losses in the West so high in Q2 2025?"
    here = await answer_why(dana, question, decompose=decompose_here)
    there = await answer_why(dana, question, decompose=dv.in_sandbox)
    assert there.draft.claims[:2] == here.draft.claims[:2]
    assert "Hail claims account for" in there.draft.claims[1].text


async def test_an_adjuster_asking_about_another_region_is_told_which_regions_they_see() -> None:
    result = await answer_why(principal_for("june"), "Why were paid losses in the West so high in Q2 2025?")
    assert result.kind == "not_allowed"
    assert result.text == "You can see claims in the North only, so I can't report on the West."
    assert not result.evidence.rows and not result.draft.claims


async def test_a_period_with_nothing_before_it_in_the_data_is_out_of_data() -> None:
    result = await answer_why(principal_for("priya"), "Why did claims go up in January 2024?")
    assert result.kind == "out_of_data"
    assert (
        result.text == "The data starts on January 1, 2024, so there is nothing before January 2024 to compare it with."
    )


async def test_a_why_question_without_a_period_asks_for_one() -> None:
    result = await answer_why(principal_for("priya"), "Why did paid losses go up?")
    assert result.kind == "clarify" and result.clarify is not None and result.clarify.options
