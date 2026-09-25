from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app import db
from app.identity import Principal
from app.live import spec
from app.live.why import answer_why_live, plan
from app.llm.fake import ScriptedLLM, calls, json_reply
from app.semantic.layer import Layer, default_layer
from app.verify import verify
from tests.live.conftest import BY_PERIL, DECOMPOSE_CODE, QUESTION, YOY_CODE, Database, NoKey, Sandbox

HEADLINE_TEXT = (
    "Paid losses in the West were $35,768,928 in Q2 2025 and $24,345,938 in Q1 2025, up $11,422,990 (46.9%)."
)
DRIVER_TEXT = "Hail claims account for 105.1% of the rise."
# In 2024 water rose and hail fell, the other way round from the change the question asks about.
WATER_LED = [
    ("current", "hail", Decimal("2000000.00")),
    ("current", "water", Decimal("9000000.00")),
    ("prior", "hail", Decimal("4000000.00")),
    ("prior", "water", Decimal("3000000.00")),
]
# A split whose groups don't add up to the change, as an analyst's suppressed cells leave one: hail is up
# $12,000,000 of the $11,422,990 rise, 105.1%, but 180.3% of the $6,654,062 these rows add up to.
SHORT = [*BY_PERIL[:1], ("current", "water", Decimal("10000000.00")), *BY_PERIL[2:]]


@dataclass
class Splits(Database):
    """The fake database, with a split by peril that depends on the year asked for, or one that falls short."""

    short: bool = False

    async def run(self, principal: Principal, query: Any, params: Any = None, **options: Any) -> db.Rows:
        text = query if isinstance(query, str) else str(query)
        if "AS peril" in text and any(isinstance(p, date) and p.year == 2024 for p in params or ()):
            self.calls.append((principal, text, params))
            return db.Rows(["period", "peril", "value"], list(WATER_LED), False, 1.0, text)
        if "AS peril" in text and self.short:
            self.calls.append((principal, text, params))
            return db.Rows(["period", "peril", "value"], list(SHORT), False, 1.0, text)
        return await super().run(principal, query, params, **options)


@pytest.fixture
def layer() -> Layer:
    return default_layer()


@pytest.fixture
def splits(monkeypatch: pytest.MonkeyPatch) -> Splits:
    fake = Splits()
    monkeypatch.setattr(db, "run", fake.run)
    return fake


def by_peril(dana: Principal, layer: Layer) -> dict[str, Any]:
    planned = plan(dana, QUESTION, None, layer)
    assert planned is not None
    return spec.to_spec(replace(planned[0], group_by=("peril",)), layer)


def moved(spec_: dict[str, Any], how: str) -> dict[str, Any]:
    if how == "period":
        return {**spec_, "period": "2024-Q2"}
    if how == "filter":
        return {**spec_, "filters": {**spec_["filters"], "channel": ["web"]}}
    if how == "limit":
        return {**spec_, "limit": 1}
    if how == "measure":
        return {**spec_, "measure": "claim_count"}
    assert how == "comparison"
    return {**spec_, "compare_to": "prior_year"}


@pytest.mark.parametrize("how", ["period", "filter", "limit", "measure", "comparison"])
async def test_a_run_whose_only_split_is_of_something_else_falls_back(
    dana: Principal, layer: Layer, splits: Splits, sandbox: Sandbox, no_key: NoKey, how: str
) -> None:
    llm = ScriptedLLM(
        calls(("query_metric", moved(by_peril(dana, layer), how))),
        calls(("analyze", {"template": "yoy", "dataset": "d2"})),
        json_reply({"code": YOY_CODE}),
        calls(("finish", {"analyses": ["a1"], "documents": []})),
    )
    done = await answer_why_live(llm, dana, QUESTION, layer=layer, sandbox=sandbox)
    # The no-key workflow names the drivers of every change it can split, so an answer that splits none falls back.
    assert (done.live, done.fallback) == (False, "invalid") and no_key.calls == [QUESTION] and llm.left == 0


async def test_a_split_of_something_else_names_no_driver_and_isnt_evidence(
    dana: Principal, layer: Layer, splits: Splits, sandbox: Sandbox, no_key: NoKey
) -> None:
    llm = ScriptedLLM(
        calls(("query_metric", by_peril(dana, layer)), ("query_metric", moved(by_peril(dana, layer), "period"))),
        calls(("analyze", {"template": "yoy", "dataset": "d3"}), ("analyze", {"template": "yoy", "dataset": "d2"})),
        json_reply({"code": YOY_CODE}),
        json_reply({"code": YOY_CODE}),
        calls(("finish", {"analyses": ["a1", "a2"], "documents": []})),
    )
    done = await answer_why_live(llm, dana, QUESTION, layer=layer, sandbox=sandbox)
    assert done.live and done.fallback is None and llm.left == 0
    # The 2024 split, where water led, names nothing; the split of the change itself names hail.
    assert [claim.text for claim in done.result.draft.claims] == [HEADLINE_TEXT, DRIVER_TEXT]
    assert verify(done.result.draft, done.result.evidence, dana).passed
    # The rows, result and statement of the other split aren't the answer's evidence. The two statements that are go
    # to the evidence panel with the values they ran with.
    evidence = done.result.evidence
    assert (len(evidence.rows), len(evidence.sandbox), len(done.result.sql)) == (6, 1, 2)
    ran = [(statement, tuple(bound)) for _, statement, bound in splits.calls]
    assert done.result.statements == [ran[0], ran[1]] and ran[2] not in done.result.statements


async def test_a_split_decomposed_as_the_orchestrator_is_told_to_names_its_driver(
    dana: Principal, layer: Layer, splits: Splits, sandbox: Sandbox, no_key: NoKey
) -> None:
    # The instructions have the orchestrator decompose every split, and a real model does, so the driver comes from
    # decompose's delta_total, adapted the way the model adapts it.
    llm = ScriptedLLM(
        calls(("query_metric", by_peril(dana, layer))),
        calls(("analyze", {"template": "decompose", "dataset": "d2"})),
        json_reply({"code": DECOMPOSE_CODE}),
        calls(("finish", {"analyses": ["a1"], "documents": []})),
    )
    done = await answer_why_live(llm, dana, QUESTION, layer=layer, sandbox=sandbox)
    assert done.live and done.fallback is None and llm.left == 0
    assert [claim.text for claim in done.result.draft.claims] == [HEADLINE_TEXT, DRIVER_TEXT]
    assert verify(done.result.draft, done.result.evidence, dana).passed


async def test_a_drivers_share_is_of_the_headlines_change_and_is_traced_to_the_rows(
    dana: Principal, layer: Layer, splits: Splits, sandbox: Sandbox, no_key: NoKey
) -> None:
    splits.short = True
    split = by_peril(dana, layer)
    llm = ScriptedLLM(
        calls(("query_metric", split)),
        calls(("analyze", {"template": "yoy", "dataset": "d2"}), ("analyze", {"template": "yoy", "dataset": "d2"})),
        json_reply({"code": YOY_CODE}),
        json_reply({"code": YOY_CODE}),
        calls(("finish", {"analyses": ["a1", "a2"], "documents": []})),
    )
    done = await answer_why_live(llm, dana, QUESTION, layer=layer, sandbox=sandbox)
    assert done.live and done.fallback is None and llm.left == 0
    # Two analyses of the same split name its driver once.
    headline, driver = done.result.draft.claims
    assert (headline.text, driver.text) == (HEADLINE_TEXT, "Hail claims account for 105.1% of the rise.")
    # d1's two rows come first, then the split's: hail now at row 2 and before at row 4.
    assert driver.numbers[0].derivation == "(value[2] - value[4]) / (value[0] - value[1])"
    verification = verify(done.result.draft, done.result.evidence, dana)
    assert verification.passed, [check.reasons for check in verification.checks]
