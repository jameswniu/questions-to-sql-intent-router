import json
import os
import subprocess
import sys
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import anthropic
import httpx2
import pytest

from app.live import analyze, documents, extract, residue, support, writer
from app.live import why as live_why
from app.live.prompts import main_system
from app.llm import client
from app.llm.client import AnthropicLLM, LiveConfigError, Refused, Truncated, Unavailable, ask, from_env
from app.llm.fake import ScriptedLLM, reply
from app.llm.request import Message, Passage, Request, Tool
from app.sandbox.client import Table
from app.semantic.layer import default_layer
from app.semantic.query import MetricQuery, Period
from tests.live.conftest import MEMO, MEMO_SENTENCE

Handler = Callable[[httpx2.Request], httpx2.Response]
REPLY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "opaque"},
        {
            "type": "text",
            "text": "Hail drove the rise.",
            "citations": [
                {
                    "type": "search_result_location",
                    "cited_text": MEMO_SENTENCE,
                    "source": MEMO.chunk_id,
                    "title": MEMO.title,
                    "search_result_index": 0,
                    "start_block_index": 1,
                    "end_block_index": 2,
                }
            ],
        },
    ],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 2000,
        "cache_creation_input_tokens": 0,
    },
}


def written(model: str = "claude-sonnet-5", **extra: Any) -> Request:
    passage = Passage(MEMO.chunk_id, MEMO.title, ("Hail memo", MEMO_SENTENCE))
    return Request("test.v1", model, main_system("Answer."), (Message("user", (passage, "Why?")),), 512, **extra)


def http(handler: Handler) -> anthropic.DefaultAsyncHttpxClient:
    return anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))


def test_live_mode_is_off_without_the_env() -> None:
    assert from_env({}) is None
    assert from_env({"LLM_BACKEND": "off", "ANTHROPIC_API_KEY": "sk-test"}) is None


def test_the_process_client_is_none_when_live_mode_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    client.default.cache_clear()
    try:
        assert client.default() is None
    finally:
        client.default.cache_clear()


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"LLM_BACKEND": "openai"}, "must be one of"),
        ({"LLM_BACKEND": "anthropic"}, "ANTHROPIC_API_KEY"),
        ({"LLM_BACKEND": "vertex"}, "VERTEX_PROJECT_ID"),
    ],
)
def test_a_live_mode_missing_its_settings_says_which(env: dict[str, str], message: str) -> None:
    with pytest.raises(LiveConfigError, match=message):
        from_env(env)


def test_the_backends_retry_twice_and_take_their_models_from_the_env() -> None:
    direct = from_env({"LLM_BACKEND": "anthropic", "ANTHROPIC_API_KEY": "sk-test", "LIVE_MAIN_MODEL": "claude-opus-5"})
    assert isinstance(direct, AnthropicLLM) and direct.provider == "anthropic"
    assert (direct.fast_model, direct.main_model) == ("claude-haiku-4-5", "claude-opus-5")
    assert isinstance(direct._client, anthropic.AsyncAnthropic) and direct._client.max_retries == 2
    vertex = from_env({"LLM_BACKEND": "vertex", "VERTEX_PROJECT_ID": "evaluateos"})
    assert isinstance(vertex, AnthropicLLM) and vertex.provider == "gcp.vertex_ai"
    assert isinstance(vertex._client, anthropic.AsyncAnthropicVertex)
    assert (vertex._client.region, vertex._client.project_id, vertex._client.max_retries) == ("global", "evaluateos", 2)
    assert (vertex.fast_model, vertex.main_model) == ("claude-haiku-4-5", "claude-sonnet-5")


def test_importing_the_live_modules_constructs_no_client() -> None:
    probe = """
import anthropic
def refuse(*args, **kwargs):
    raise SystemExit("a client was constructed on import")
anthropic.AsyncAnthropic.__init__ = refuse
anthropic.AsyncAnthropicVertex.__init__ = refuse
import app.live, app.live.why, app.live.residue, app.live.extract, app.live.writer, app.live.support
import app.llm.client, app.llm.fake
assert app.llm.client.default.cache_info().currsize == 0
print("ok")
"""
    env = {**os.environ, "LLM_BACKEND": "anthropic", "ANTHROPIC_API_KEY": "sk-test"}
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=120)
    assert done.stdout.strip() == "ok", done.stderr[-2000:]


async def test_the_anthropic_backend_normalizes_text_citations_usage_and_the_turn_to_echo() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=REPLY)

    api = anthropic.AsyncAnthropic(api_key="sk-test", max_retries=0, http_client=http(handler))
    llm = AnthropicLLM(api, fast_model="claude-haiku-4-5", main_model="claude-sonnet-5", provider="anthropic")
    request = written(cache=True)
    response = await llm.complete(request)
    assert sent[0] == request.params()
    [text] = response.texts
    assert text.text == "Hail drove the rise." and text.citations[0].source == MEMO.chunk_id
    assert (text.citations[0].index, text.citations[0].start, text.citations[0].end) == (0, 1, 2)
    assert (response.usage.input_tokens, response.usage.cache_read, response.usage.cache_write) == (100, 2000, 0)
    # Sonnet 5 lists at $2 in, $10 out and $0.20 for a cache read, per million tokens.
    assert response.usage.cost_usd == Decimal("0.0008")
    assert response.model == "claude-sonnet-5" and response.prompt_hash == request.prompt_hash
    assert response.turn == (
        {"type": "thinking", "thinking": "", "signature": "opaque"},
        {"type": "text", "text": "Hail drove the rise."},
    )


def test_cost_prices_a_dated_model_id_from_its_undated_name() -> None:
    usage = client.Usage(input_tokens=1000, output_tokens=200)
    # Haiku lists at $1 in and $5 out, per million tokens.
    assert client.cost("claude-haiku-4-5-20251001", usage) == Decimal("0.002")
    assert client.cost("some-unpriced-model-20251001", usage) is None


async def test_the_vertex_backend_calls_the_model_in_the_project_and_region() -> None:
    urls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        urls.append(str(request.url))
        return httpx2.Response(200, json={**REPLY, "content": [{"type": "text", "text": "OK"}]})

    api = anthropic.AsyncAnthropicVertex(
        project_id="evaluateos", region="global", access_token="token", max_retries=0, http_client=http(handler)
    )
    llm = AnthropicLLM(api, fast_model="claude-haiku-4-5", main_model="claude-sonnet-5", provider="gcp.vertex_ai")
    await llm.complete(written("claude-haiku-4-5"))
    assert urls == [
        "https://aiplatform.googleapis.com/v1/projects/evaluateos/locations/global/publishers/anthropic/models/"
        "claude-haiku-4-5:rawPredict"
    ]


async def test_an_api_error_becomes_unavailable_and_keeps_its_cause() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "quota"}})

    api = anthropic.AsyncAnthropic(api_key="sk-test", max_retries=0, http_client=http(handler))
    llm = AnthropicLLM(api, fast_model="claude-haiku-4-5", main_model="claude-sonnet-5", provider="anthropic")
    with pytest.raises(Unavailable) as raised:
        await llm.complete(written())
    assert isinstance(raised.value.__cause__, anthropic.RateLimitError)


@pytest.mark.parametrize(("stop", "error"), [("refusal", Refused), ("max_tokens", Truncated)])
async def test_a_refusal_or_a_cut_off_reply_raises_a_typed_error(stop: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        await ask(ScriptedLLM(reply('{"route": "wh', stop_reason=stop)), written())


def test_document_text_is_cited_unless_the_request_wants_a_structured_output() -> None:
    free = written().params()["messages"][0]["content"][0]
    assert free["type"] == "search_result" and free["citations"] == {"enabled": True}
    assert (free["source"], free["title"]) == (MEMO.chunk_id, MEMO.title)
    shaped = support.request("claude-haiku-4-5", "Hail drove it.", [MEMO]).params()
    assert shaped["messages"][0]["content"][0]["citations"] == {"enabled": False}
    assert "format" in shaped["output_config"]


def test_no_request_builder_ever_sets_a_sampling_parameter() -> None:
    layer = default_layer()
    mq = MetricQuery("paid_losses", period=Period.quarter(2025, 2), compare_to="prior_period")
    table = Table(["period", "value"], [["current", 1.0], ["prior", 2.0]])
    orchestrator = Request(
        live_why.TEMPLATE, "claude-sonnet-5", ("s",), (Message("user", ("q",)),), 64, tools=live_why.tools(layer)
    )
    made = [
        orchestrator,
        residue.request("claude-haiku-4-5", "banana"),
        extract.request("claude-haiku-4-5", "what did we shell out in 2025", layer),
        support.request("claude-haiku-4-5", "Hail drove it.", [MEMO]),
        documents.request("claude-sonnet-5", mq, layer, [MEMO], ["peril:hail"]),
        analyze.request("claude-sonnet-5", "yoy", table, {"value": "value"}),
        writer.request("claude-sonnet-5", "q", [MEMO], (), template="w", instructions="i", timeout_s=5),
        writer.request(
            "claude-sonnet-5", "q", [MEMO], ("Paid losses rose.",), template="w", instructions="i", timeout_s=5
        ),
    ]
    for request in made:
        assert not {"temperature", "top_p", "top_k"} & set(request.params())


def test_the_main_model_caches_its_system_prefix_and_the_fast_model_is_never_marked() -> None:
    main = written(cache=True).params()
    assert main["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in block for block in main["system"][:-1])
    # Haiku 4.5 caches nothing under 4096 tokens, and the fast prompts are shorter than that.
    layer = default_layer()
    for request in (residue.request("claude-haiku-4-5", "banana"), extract.request("claude-haiku-4-5", "q", layer)):
        assert not request.cached and "cache_control" not in json.dumps(request.params())
    assert not written("claude-haiku-4-5", cache=True).cached


def test_the_prompt_hash_names_the_prompt_and_never_the_question() -> None:
    tool = Tool("finish", "Ends the work.", {"type": "object", "properties": {}, "additionalProperties": False})

    def made(question: str, template: str = "t.v1", instructions: str = "Answer.") -> Request:
        return Request(template, "claude-sonnet-5", (instructions,), (Message("user", (question,)),), 64, tools=(tool,))

    assert made("Why?").prompt_hash == made("Something else entirely?").prompt_hash
    assert made("Why?").prompt_hash != made("Why?", template="t.v2").prompt_hash
    assert made("Why?").prompt_hash != made("Why?", instructions="Answer briefly.").prompt_hash


@pytest.mark.parametrize(
    "answer",
    [
        httpx2.Response(200, content=b"not json", headers={"content-type": "application/json"}),
        httpx2.Response(
            200, json={**REPLY, "content": [{"type": "server_tool_use", "id": "s", "name": "x", "input": {}}]}
        ),
        httpx2.Response(200, json={**REPLY, "content": [{"type": "mystery", "text": None}]}),
    ],
)
async def test_a_reply_the_app_cant_read_becomes_unavailable(answer: httpx2.Response) -> None:
    api = anthropic.AsyncAnthropic(api_key="sk-test", max_retries=0, http_client=http(lambda request: answer))
    llm = AnthropicLLM(api, fast_model="claude-haiku-4-5", main_model="claude-sonnet-5", provider="anthropic")
    with pytest.raises(Unavailable):
        await llm.complete(written())
