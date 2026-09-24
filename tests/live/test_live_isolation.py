import json
from dataclasses import replace
from typing import Any

import pytest

from app.answer.why import NO_DOCUMENT
from app.identity import Principal
from app.live import analyze, documents, spec, support, writer
from app.live import why as live_why
from app.live.why import answer_why_live, plan
from app.llm.client import Response
from app.llm.facts import IsolationViolation, Token
from app.llm.fake import ScriptedLLM, calls, cited, json_reply, reply
from app.llm.request import Message, Passage, Request, Tool, ToolResult, Turn
from app.semantic.layer import Layer, default_layer
from app.verify import COULD_NOT_CONFIRM, finalize, verify
from tests.live.conftest import (
    EAST_CANARY,
    MEMO,
    MEMO_SENTENCE,
    NOTE_TEXT,
    PLANTED,
    PLANTED_HIT,
    QUESTION,
    YOY_CODE,
    Database,
    NoKey,
    Sandbox,
    Search,
    rendered,
)

HEADLINE_TEXT = (
    "Paid losses in the West were $35,768,928 in Q2 2025 and $24,345,938 in Q1 2025, up $11,422,990 (46.9%)."
)
DRIVER_TEXT = "Hail claims account for 105.1% of the rise."
# Nothing from a document may appear in a request that offers tools: its words, title, header, doc id or chunk id
# (whose anchor is a heading's words), the note, or the canary.
DOCUMENT_TEXT = (
    *(MEMO_SENTENCE, PLANTED, NOTE_TEXT, EAST_CANARY),
    *(MEMO.title, PLANTED_HIT.title, "Hail memo |", "Operations memo |"),
    *(MEMO.doc_id, PLANTED_HIT.doc_id, MEMO.chunk_id, PLANTED_HIT.chunk_id, MEMO.anchor, PLANTED_HIT.anchor),
)
SUPPORTED = json_reply({"supported": True, "reason": "The memo says so."})
UNSUPPORTED = json_reply({"supported": False, "reason": "The memo doesn't say this."})
CLOUD_SEEDING = "The storm was caused by a competitor's cloud seeding program."
PICKS = json_reply({"picks": [{"chunk_id": MEMO.chunk_id, "driver": "peril:hail", "relevance": "explains"}]})


@pytest.fixture
def layer() -> Layer:
    return default_layer()


def by_peril(dana: Principal, layer: Layer) -> dict[str, Any]:
    planned = plan(dana, QUESTION, None, layer)
    assert planned is not None
    return spec.to_spec(replace(planned[0], group_by=("peril",)), layer)


def script(dana: Principal, layer: Layer, selection: Response, named: list[str], *after: Response) -> ScriptedLLM:
    """The orchestrator's three turns, with the analysis and docs calls in the order the run makes them, then
    whatever follows finish: the writer and the reading, when documents are named."""
    return ScriptedLLM(
        calls(("query_metric", by_peril(dana, layer))),
        calls(("analyze", {"template": "yoy", "dataset": "d2"}), ("find_documents", {"drivers": ["peril:hail"]})),
        json_reply({"code": YOY_CODE}),
        selection,
        calls(("finish", {"analyses": ["a1"], "documents": named})),
        *after,
    )


async def test_a_full_live_run_keeps_document_text_out_of_every_request_that_offers_tools(
    dana: Principal, layer: Layer, database: Database, search: Search, sandbox: Sandbox, no_key: NoKey
) -> None:
    llm = script(dana, layer, PICKS, ["c1"], reply(cited(MEMO_SENTENCE, (MEMO.chunk_id, 0))), SUPPORTED)
    done = await answer_why_live(llm, dana, QUESTION, layer=layer, sandbox=sandbox)
    assert done.live and done.fallback is None and not no_key.calls and llm.left == 0
    assert [r.template for r in llm.requests] == [
        live_why.TEMPLATE,
        live_why.TEMPLATE,
        analyze.TEMPLATE,
        documents.TEMPLATE,
        live_why.TEMPLATE,
        writer.WHY_TEMPLATE,
        support.TEMPLATE,
    ]
    requests = rendered(llm)
    for tools, passages, body in requests:
        if tools:
            assert not [text for text in DOCUMENT_TEXT if text in body]
        if passages:
            assert not tools
        assert not {"temperature", "top_p", "top_k"} & set(json.loads(body))
    assert all(NOTE_TEXT not in body for _, _, body in requests), "the note isn't a memo or bulletin"
    # The figures are written without a model, the cited sentence passes its reading, and all of it the verifier.
    verification = verify(done.result.draft, done.result.evidence, dana, second_check=done.second_check)
    assert [check.claim.text for check in verification.checks] == [HEADLINE_TEXT, DRIVER_TEXT, MEMO_SENTENCE]
    assert verification.passed, [check.reasons for check in verification.checks]
    assert verification.checks[2].sources == (MEMO,)
    assert done.answer is not None and not done.retried
    assert done.answer.text == f"{HEADLINE_TEXT} {DRIVER_TEXT} {MEMO_SENTENCE}"
    # Every statement the answer rests on goes to the evidence panel with the values bound to it.
    assert [statement for statement, _ in done.result.statements] == list(done.result.sql)
    assert all(bound for _, bound in done.result.statements) and len(done.result.params) == len(done.result.sql) == 2


async def test_the_writer_can_neither_place_a_figure_nor_state_an_unread_cause(
    dana: Principal, layer: Layer, database: Database, search: Search, sandbox: Sandbox, no_key: NoKey
) -> None:
    first = reply(
        "Paid losses in the West were $24,345,938 in Q2 2025 and $35,768,928 in Q1 2025. ",
        "Water claims account for $12,000,000 of the rise. ",
        "Adjuster Bob Ruiz in the East committed fraud; see evil.example. ",
        cited(MEMO_SENTENCE, (MEMO.chunk_id, 0)),
        cited(f" {CLOUD_SEEDING}", (MEMO.chunk_id, 0)),
    )
    second = reply(cited(MEMO_SENTENCE, (MEMO.chunk_id, 0)))
    llm = script(dana, layer, PICKS, ["c1"], first, SUPPORTED, UNSUPPORTED, second)
    done = await answer_why_live(llm, dana, QUESTION, layer=layer, sandbox=sandbox)
    assert done.live and done.fallback is None and done.retried and llm.left == 0
    # Only the two cited sentences were read. The one the reading didn't bear out went back to the writer with the
    # verifier's reason, in a request with the documents and no tools, and the second writing left it out.
    assert [r.template for r in llm.requests].count(support.TEMPLATE) == 2
    retry = next(r for r in llm.requests if r.template == writer.WHY_RETRY_TEMPLATE)
    assert not retry.tools and [p.source for p in retry.passages] == [MEMO.chunk_id]
    [feedback] = [part for part in retry.messages[0].parts if isinstance(part, str) and "didn't hold up" in part]
    assert f'"{CLOUD_SEEDING}": reading' in feedback
    assert [claim.text for claim in done.result.draft.claims] == [HEADLINE_TEXT, DRIVER_TEXT, MEMO_SENTENCE]
    assert done.answer is not None and not done.answer.claims_cut
    assert done.answer.text == f"{HEADLINE_TEXT} {DRIVER_TEXT} {MEMO_SENTENCE}"
    for placed in ("$24,345,938 in Q2", "$12,000,000", "Bob Ruiz", "evil.example", "cloud seeding"):
        assert placed not in done.answer.text


async def test_a_cause_the_second_writing_still_states_is_cut(
    dana: Principal, layer: Layer, database: Database, search: Search, sandbox: Sandbox, no_key: NoKey
) -> None:
    both = reply(cited(MEMO_SENTENCE, (MEMO.chunk_id, 0)), cited(f" {CLOUD_SEEDING}", (MEMO.chunk_id, 0)))
    llm = script(dana, layer, PICKS, ["c1"], both, SUPPORTED, UNSUPPORTED, both)
    done = await answer_why_live(llm, dana, QUESTION, layer=layer, sandbox=sandbox)
    assert done.live and done.retried and llm.left == 0 and done.answer is not None
    assert done.answer.text == f"{HEADLINE_TEXT} {DRIVER_TEXT} {MEMO_SENTENCE}"
    assert [claim.text for claim in done.answer.claims_cut] == [CLOUD_SEEDING]
    assert done.answer.could_not_confirm == (COULD_NOT_CONFIRM["reading"],)
    # The draft is the one the verifier finished, so checking it with its reading gives the same answer.
    again = finalize(
        done.result.draft, verify(done.result.draft, done.result.evidence, dana, second_check=done.second_check)
    )
    assert again == done.answer


async def test_a_reading_that_bears_out_no_cited_sentence_falls_back(
    dana: Principal, layer: Layer, database: Database, search: Search, sandbox: Sandbox, no_key: NoKey
) -> None:
    written = reply(cited(CLOUD_SEEDING, (MEMO.chunk_id, 0)))
    llm = script(dana, layer, PICKS, ["c1"], written, UNSUPPORTED, written)
    done = await answer_why_live(llm, dana, QUESTION, layer=layer, sandbox=sandbox)
    assert (done.live, done.fallback) == (False, "reading") and no_key.calls == [QUESTION] and llm.left == 0


async def test_a_planted_instruction_the_docs_sub_agent_obeys_is_rejected_and_never_reaches_the_orchestrator(
    dana: Principal, layer: Layer, database: Database, search: Search, sandbox: Sandbox, no_key: NoKey
) -> None:
    obeyed = reply(f"Sure. I will {PLANTED}: North, South, East and West. SSN 123-45-6789.")
    llm = script(dana, layer, obeyed, [])
    done = await answer_why_live(llm, dana, QUESTION, layer=layer, sandbox=sandbox)
    assert done.live and done.fallback is None and llm.left == 0
    docs_at = next(i for i, r in enumerate(llm.requests) if r.template == documents.TEMPLATE)
    assert PLANTED in rendered(llm)[docs_at][2] and not llm.requests[docs_at].tools
    later = [r for r in llm.requests[docs_at:] if r.template == live_why.TEMPLATE]
    assert later, "the orchestrator carries on after the rejected selection"
    for request in later:
        body = json.dumps(request.params())
        assert PLANTED not in body and "SSN" not in body and "123-45-6789" not in body
    handed_back = json.loads(later[0].params()["messages"][-1]["content"][-1]["content"])
    assert handed_back == {"status": "none_valid", "documents": [], "dropped": 1}
    assert done.result.draft.caveats == (NO_DOCUMENT,)


def test_the_request_builder_refuses_tools_beside_document_text() -> None:
    passage = Passage(MEMO.chunk_id, MEMO.title, (MEMO_SENTENCE,))
    tool = Tool("query_metric", "Runs a query.", {"type": "object", "properties": {}, "additionalProperties": False})
    with pytest.raises(IsolationViolation):
        Request("t", "claude-sonnet-5", ("s",), (Message("user", (passage, "Why?")),), 100, tools=(tool,))


def test_an_echoed_turn_carrying_citations_is_refused() -> None:
    with pytest.raises(IsolationViolation):
        Turn(({"type": "text", "text": MEMO_SENTENCE, "citations": []},))


@pytest.mark.parametrize(
    "content",
    [
        {"text": MEMO_SENTENCE},
        {"rows": [{"note": "a free text value"}]},
        {"Bad Key": 1},
        [float("nan")],
        {"driver": "peril:hail"},
    ],
)
def test_a_tool_result_carries_only_ids_enums_and_numbers(content: Any) -> None:
    with pytest.raises(IsolationViolation):
        ToolResult("toolu_1", content)


@pytest.mark.parametrize("text", [PLANTED, f"memo {EAST_CANARY}", EAST_CANARY, PLANTED_HIT.anchor[:40], "hail-event"])
def test_a_token_without_a_vocabulary_is_only_a_handle_a_period_or_a_status(text: str) -> None:
    with pytest.raises(IsolationViolation):
        Token(text)
    assert Token.word("peril:hail", ("peril:hail",)).value == "peril:hail"
    assert [Token(ok).value for ok in ("d1", "2025-Q2", "2025-04-01", "none_valid")]
