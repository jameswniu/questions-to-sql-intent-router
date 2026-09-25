import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import httpx
import pytest
from google import genai
from google.auth.exceptions import DefaultCredentialsError
from google.genai import errors, types

from app.answer.types import Evidence
from app.identity import principal_for
from app.live import support, writer
from app.llm import client
from app.llm.client import (
    AnthropicLLM,
    LiveConfigError,
    LLMError,
    Refused,
    Text,
    Truncated,
    Unavailable,
    ask,
    from_env,
    output_for,
    parse,
)
from app.llm.fake import ScriptedLLM, cited, json_reply, reply
from app.llm.gemini import THINKING_ROOM, GeminiLLM, config, contents, stop_reason
from app.llm.request import Effort, Message, Passage, Request, Tool, ToolResult, Turn
from tests.live.conftest import MEMO, MEMO_SENTENCE, make_hit
from tools import live_check

GEMINI = "gemini-3.8-flash"
VERDICT = '{"supported": true, "reason": "The memo says so."}'
SUPPORTED = json_reply({"supported": True, "reason": "Section 3 says so."})
UNSUPPORTED = json_reply({"supported": False, "reason": "The text gives a $1,000 deductible."})
VERTEX = {"LLM_BACKEND": "vertex", "VERTEX_PROJECT_ID": "evaluateos"}
DIRECT = {"LLM_BACKEND": "anthropic", "ANTHROPIC_API_KEY": "sk-test"}
TOOL = Tool("finish", "Ends the work.", {"type": "object", "properties": {}, "additionalProperties": False})
WORDING = make_hit(
    "ho-2025#section-3:1",
    "Section 3 covers a sudden and accidental discharge of water from a plumbing system. "
    "The all peril deductible is $1,000.",
    kind="wording",
    title="Form HO-2025",
)
KEPT = "Burst pipes are covered under Section 3."
STRETCHED = "Burst pipes are covered with no deductible."
REFUSED_FINISHES = ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION")

type Answer = types.GenerateContentResponse | Exception | Callable[[], Awaitable[types.GenerateContentResponse]]


@dataclass
class Models:
    """Stands in for the SDK's async models API: answers every call the same way and keeps what each was sent."""

    answer: Answer
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def generate_content(
        self, *, model: str, contents: list[types.ContentUnion], config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        if isinstance(self.answer, Exception):
            raise self.answer
        if isinstance(self.answer, types.GenerateContentResponse):
            return self.answer
        return await self.answer()


def gemini(answer: Answer) -> tuple[GeminiLLM, Models]:
    models = Models(answer)
    return GeminiLLM(cast(genai.Client, SimpleNamespace(aio=SimpleNamespace(models=models)))), models


def answered(
    text: str = VERDICT,
    *,
    finish: str = "STOP",
    usage: dict[str, int] | None = None,
    model: str | None = GEMINI,
    thought: str = "",
) -> types.GenerateContentResponse:
    """A reply as Vertex sends it: one candidate, its thinking first when it thought, and the usage."""
    parts = [{"text": thought, "thought": True}] if thought else []
    body: dict[str, Any] = {
        "candidates": [{"content": {"role": "model", "parts": [*parts, {"text": text}]}, "finishReason": finish}],
        "usageMetadata": usage or {"promptTokenCount": 228, "candidatesTokenCount": 62},
    }
    if model:
        body["modelVersion"] = model
    return types.GenerateContentResponse.model_validate(body)


BLOCKED = types.GenerateContentResponse.model_validate(
    {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}, "usageMetadata": {"promptTokenCount": 228}}
)


def checking(statement: str = "Hail drove the rise.") -> Request:
    return support.request(GEMINI, statement, [MEMO])


def test_gemini_takes_text_and_passages_numbered_as_the_api_numbers_search_results_in_its_own_roles() -> None:
    first = Passage(MEMO.chunk_id, MEMO.title, ("Hail memo | hail-event", MEMO_SENTENCE))
    second = Passage(WORDING.chunk_id, WORDING.title, ("The all peril deductible is $1,000.",))
    request = Request(
        "t.v1",
        GEMINI,
        ("Check it.",),
        (
            Message("user", (first, "Statement: Hail drove the rise.")),
            Message("assistant", ("Supported.",)),
            Message("user", (second, "Statement: There is no deductible.")),
        ),
        64,
        output=output_for(support.Verdict),
    )
    made = contents(request)
    assert [content.role for content in made] == ["user", "model", "user"]
    assert [[part.text for part in content.parts or ()] for content in made] == [
        [
            f"Search result 0 (source {MEMO.chunk_id}): {MEMO.title}\nHail memo | hail-event\n{MEMO_SENTENCE}",
            "Statement: Hail drove the rise.",
        ],
        ["Supported."],
        [
            f"Search result 1 (source {WORDING.chunk_id}): {WORDING.title}\nThe all peril deductible is $1,000.",
            "Statement: There is no deductible.",
        ],
    ]
    assert request.passages == (first, second)


@pytest.mark.parametrize(
    ("messages", "tools"),
    [
        ((Message("user", ("Why?",)),), (TOOL,)),
        ((Message("user", (Passage(MEMO.chunk_id, MEMO.title, (MEMO_SENTENCE,)), "Why?")),), ()),
        ((Message("user", (ToolResult("toolu_1", {"rows": 3}),)),), ()),
        ((Message("user", ("Why?",)), Message("assistant", (Turn(({"type": "text", "text": "Hail."},)),))), ()),
    ],
    ids=["tools", "citable passages", "tool result", "echoed turn"],
)
async def test_a_request_only_claude_can_serve_is_refused_before_anything_is_sent(
    messages: tuple[Message, ...], tools: tuple[Tool, ...]
) -> None:
    llm, models = gemini(answered())
    with pytest.raises(Unavailable):
        await llm.complete(Request("t.v1", GEMINI, ("s",), messages, 64, tools=tools))
    assert models.calls == []


@pytest.mark.parametrize(
    ("effort", "level"),
    [
        (None, types.ThinkingLevel.LOW),
        ("low", types.ThinkingLevel.LOW),
        ("medium", types.ThinkingLevel.MEDIUM),
        ("high", types.ThinkingLevel.HIGH),
    ],
)
def test_the_call_thinks_low_unless_the_request_asks_for_more(
    effort: Effort | None, level: types.ThinkingLevel
) -> None:
    assert config(replace(checking(), effort=effort)).thinking_config == types.ThinkingConfig(thinking_level=level)


def test_the_call_carries_the_system_text_token_limit_and_schema_and_nothing_to_sample_with() -> None:
    settings = config(checking())
    assert settings.system_instruction == support.INSTRUCTIONS
    assert settings.max_output_tokens == 256 + THINKING_ROOM["low"]
    assert settings.response_mime_type == "application/json"
    assert settings.response_json_schema == dict(output_for(support.Verdict).schema)
    assert settings.automatic_function_calling == types.AutomaticFunctionCallingConfig(disable=True)
    assert set(settings.model_dump(exclude_none=True)) == {
        "system_instruction",
        "max_output_tokens",
        "thinking_config",
        "response_mime_type",
        "response_json_schema",
        "automatic_function_calling",
    }
    free = config(Request("t.v1", GEMINI, ("One.", "Two."), (Message("user", ("q",)),), 64))
    assert free.system_instruction == "One.\n\nTwo."
    assert (free.response_mime_type, free.response_json_schema) == (None, None)
    assert config(Request("t.v1", GEMINI, (), (Message("user", ("q",)),), 64)).system_instruction is None


@pytest.mark.parametrize("effort", [None, "low", "medium", "high"])
def test_the_token_limit_is_the_requests_reply_limit_plus_room_to_think_at_its_level(effort: Effort | None) -> None:
    request = replace(checking(), effort=effort)
    assert config(request).max_output_tokens == request.max_tokens + THINKING_ROOM[effort or "low"]


async def test_a_verdict_after_as_much_thinking_as_a_real_check_took_still_fits() -> None:
    """Gemini counts thinking inside max_output_tokens. A real support check thought for 232 tokens at low, and with
    the limit at the check's own 256 its verdict stopped 6 tokens in, as '{"supported": false, "'."""
    thought, verdict = 232, 62

    async def thinking() -> types.GenerateContentResponse:
        limit = models.calls[-1]["config"].max_output_tokens
        if thought + verdict > limit:
            usage = {"promptTokenCount": 344, "candidatesTokenCount": limit - thought, "thoughtsTokenCount": thought}
            return answered('{"supported": false, "', finish="MAX_TOKENS", usage=usage)
        usage = {"promptTokenCount": 344, "candidatesTokenCount": verdict, "thoughtsTokenCount": thought}
        return answered(usage=usage, thought="The memo names the hail.")

    llm, models = gemini(thinking)
    assert await support.check(llm, "Hail drove the rise.", [MEMO]) == support.Verdict(
        supported=True, reason="The memo says so."
    )


@pytest.mark.parametrize(
    ("finish", "stop"),
    [
        ("STOP", "end_turn"),
        ("MAX_TOKENS", "max_tokens"),
        *((finish, "refusal") for finish in REFUSED_FINISHES),
        ("OTHER", "other"),
        ("MALFORMED_FUNCTION_CALL", "malformed_function_call"),
    ],
)
def test_each_finish_reason_becomes_a_stop_reason_ask_reads(finish: str, stop: str) -> None:
    assert stop_reason(answered(finish=finish)) == stop


def test_a_prompt_gemini_blocked_is_a_refusal_and_a_reply_with_no_candidate_is_not() -> None:
    assert stop_reason(BLOCKED) == "refusal"
    assert stop_reason(types.GenerateContentResponse()) == "no_candidate"


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (answered(finish="SAFETY"), Refused),
        (BLOCKED, Refused),
        (answered(finish="MAX_TOKENS"), Truncated),
        (answered(finish="OTHER"), LLMError),
    ],
    ids=["filtered", "blocked", "cut off", "other"],
)
async def test_ask_raises_for_a_reply_gemini_didnt_finish(
    response: types.GenerateContentResponse, error: type[LLMError]
) -> None:
    llm, _ = gemini(response)
    with pytest.raises(error) as raised:
        await ask(llm, checking())
    assert type(raised.value) is error


async def test_usage_is_counted_the_way_anthropic_reports_it_and_priced_at_the_vertex_rate() -> None:
    usage = {
        "promptTokenCount": 1000,
        "cachedContentTokenCount": 400,
        "candidatesTokenCount": 50,
        "thoughtsTokenCount": 30,
    }
    llm, models = gemini(answered(usage=usage, thought="The memo names the hail."))
    request = checking()
    response = await llm.complete(request)
    # Gemini counts the cached tokens inside the prompt, and bills thinking as output.
    assert (response.usage.input_tokens, response.usage.output_tokens) == (600, 80)
    assert (response.usage.cache_read, response.usage.cache_write) == (400, 0)
    # $0.75 in, $3.75 out and $0.075 a cached token, per million: 600 x 0.75 + 80 x 3.75 + 400 x 0.075.
    assert response.usage.cost_usd == Decimal("0.00078")
    # The thinking stays out of the text, and a reading cites nothing and echoes no turn.
    assert response.texts == (Text(VERDICT),) and response.tool_calls == () and response.turn == ()
    assert (response.stop_reason, response.model, response.prompt_hash) == ("end_turn", GEMINI, request.prompt_hash)
    assert parse(response, support.Verdict) == support.Verdict(supported=True, reason="The memo says so.")
    [call] = models.calls
    assert (call["model"], call["contents"], call["config"]) == (GEMINI, contents(request), config(request))


async def test_a_reply_that_names_no_model_takes_the_requested_one_and_an_unknown_model_has_no_cost() -> None:
    unnamed, _ = gemini(answered(model=None))
    assert (await unnamed.complete(checking())).model == GEMINI
    unknown, _ = gemini(answered(model="gemini-9-ultra"))
    response = await unknown.complete(checking())
    assert response.model == "gemini-9-ultra" and response.usage.cost_usd is None


@pytest.mark.parametrize(
    "failure",
    [
        errors.ClientError(429, {"error": {"code": 429, "message": "Quota exceeded.", "status": "RESOURCE_EXHAUSTED"}}),
        errors.ServerError(503, {"error": {"code": 503, "message": "Overloaded.", "status": "UNAVAILABLE"}}),
        DefaultCredentialsError("Your default credentials were not found."),  # type: ignore[no-untyped-call]
        httpx.ConnectError("Connection refused"),
        OSError("Network is unreachable"),
        ValueError("The body wasn't JSON"),
    ],
    ids=["rate limit", "server error", "no credentials", "dropped connection", "network", "unreadable body"],
)
async def test_an_api_auth_or_network_error_becomes_unavailable_and_keeps_its_cause(failure: Exception) -> None:
    llm, _ = gemini(failure)
    with pytest.raises(Unavailable, match="^support.v1: ") as raised:
        await llm.complete(checking())
    assert raised.value.__cause__ is failure


async def test_a_call_past_its_timeout_becomes_unavailable() -> None:
    async def hang() -> types.GenerateContentResponse:
        await asyncio.sleep(30)
        raise AssertionError("the timeout should have stopped this call")

    llm, models = gemini(hang)
    started = time.monotonic()
    with pytest.raises(Unavailable) as raised:
        await llm.complete(replace(checking(), timeout_s=0.05))
    assert isinstance(raised.value.__cause__, TimeoutError) and len(models.calls) == 1
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("setting", [None, "", "same", " Same "])
def test_the_live_client_checks_for_itself_unless_told_otherwise(setting: str | None) -> None:
    llm = from_env(VERTEX if setting is None else {**VERTEX, "LLM_CHECK_BACKEND": setting, "GEMINI_PROJECT": "p"})
    assert isinstance(llm, AnthropicLLM) and llm.checker is llm


def test_gemini_checks_on_vertex_in_the_live_project_unless_given_its_own() -> None:
    llm = from_env({**VERTEX, "LLM_CHECK_BACKEND": "gemini"})
    assert llm is not None
    checker = llm.checker
    assert isinstance(checker, GeminiLLM) and checker.checker is checker
    assert (checker.provider, checker.fast_model, checker.main_model) == ("gcp.vertex_ai", GEMINI, GEMINI)
    api = checker._client._api_client
    retries = api._http_options.retry_options
    assert (api.vertexai, api.project, api.location) == (True, "evaluateos", "global")
    assert retries is not None and retries.attempts == 4
    # The main client is unchanged: Claude on Vertex writes, with its own fast and main models.
    assert (llm.provider, llm.fast_model, llm.main_model) == ("gcp.vertex_ai", "claude-haiku-4-5", "claude-sonnet-5")
    own = from_env(
        {
            **DIRECT,
            "LLM_CHECK_BACKEND": "Gemini",
            "GEMINI_PROJECT": "checks",
            "GEMINI_LOCATION": "us-central1",
            "LIVE_CHECK_MODEL": "gemini-3.8-pro",
        }
    )
    assert own is not None and isinstance(own.checker, GeminiLLM) and own.checker.fast_model == "gemini-3.8-pro"
    api = own.checker._client._api_client
    assert (api.project, api.location) == ("checks", "us-central1")


def test_a_claude_checker_is_a_second_client_on_its_backend_with_its_own_model() -> None:
    llm = from_env({**VERTEX, "LLM_CHECK_BACKEND": "anthropic", "ANTHROPIC_API_KEY": "sk-test"})
    assert llm is not None
    checker = llm.checker
    assert isinstance(checker, AnthropicLLM) and checker is not llm and checker.checker is checker
    assert isinstance(checker._client, anthropic.AsyncAnthropic) and checker._client.max_retries == 2
    assert checker.provider == "anthropic" and checker.fast_model == checker.main_model == "claude-haiku-4-5"
    on_vertex = from_env(
        {
            **DIRECT,
            "LIVE_FAST_MODEL": "claude-sonnet-5",
            "LLM_CHECK_BACKEND": "vertex",
            "VERTEX_PROJECT_ID": "evaluateos",
            "VERTEX_REGION": "global",
            "LIVE_CHECK_MODEL": "claude-opus-5",
        }
    )
    assert on_vertex is not None and on_vertex.fast_model == "claude-sonnet-5"
    checker = on_vertex.checker
    assert isinstance(checker, AnthropicLLM) and isinstance(checker._client, anthropic.AsyncAnthropicVertex)
    assert (checker._client.project_id, checker._client.region, checker._client.max_retries) == (
        "evaluateos",
        "global",
        2,
    )
    assert (checker.provider, checker.fast_model) == ("gcp.vertex_ai", "claude-opus-5")


@pytest.mark.parametrize(
    ("env", "message"),
    [
        (
            {**DIRECT, "LLM_CHECK_BACKEND": "gemini"},
            "LLM_CHECK_BACKEND=gemini needs GEMINI_PROJECT or VERTEX_PROJECT_ID",
        ),
        ({**VERTEX, "LLM_CHECK_BACKEND": "anthropic"}, "LLM_CHECK_BACKEND=anthropic needs ANTHROPIC_API_KEY"),
        ({**DIRECT, "LLM_CHECK_BACKEND": "vertex"}, "LLM_CHECK_BACKEND=vertex needs VERTEX_PROJECT_ID"),
        (
            {**VERTEX, "LLM_CHECK_BACKEND": "openai"},
            "LLM_CHECK_BACKEND must be one of same, gemini, anthropic, vertex, not 'openai'",
        ),
    ],
)
def test_a_checker_missing_its_settings_says_which(env: dict[str, str], message: str) -> None:
    with pytest.raises(LiveConfigError, match=re.escape(message)):
        from_env(env)


@pytest.mark.parametrize("setting", ["gemini", "openai"])
def test_a_check_backend_without_a_live_backend_means_nothing(setting: str) -> None:
    assert from_env({"LLM_CHECK_BACKEND": setting}) is None
    assert from_env({"LLM_BACKEND": "off", "LLM_CHECK_BACKEND": setting, "GEMINI_PROJECT": "evaluateos"}) is None


def test_the_process_client_logs_where_its_checks_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for name in ("LIVE_FAST_MODEL", "LIVE_MAIN_MODEL", "LIVE_CHECK_MODEL", "GEMINI_PROJECT", "GEMINI_LOCATION"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {**VERTEX, "LLM_CHECK_BACKEND": "gemini"}.items():
        monkeypatch.setenv(name, value)
    client.default.cache_clear()
    try:
        with caplog.at_level(logging.INFO, logger="app.llm.client"):
            llm = client.default()
        assert llm is not None and isinstance(llm.checker, GeminiLLM)
        assert f"main model claude-sonnet-5, checker gcp.vertex_ai {GEMINI}" in caplog.text
    finally:
        client.default.cache_clear()


async def test_the_support_check_reads_on_the_checker_with_the_checkers_fast_model() -> None:
    reader = ScriptedLLM(SUPPORTED, fast_model=GEMINI, main_model=GEMINI)
    llm = ScriptedLLM(checker=reader)
    assert (await support.check(llm, KEPT, [WORDING])).supported
    [request] = reader.sent(support.TEMPLATE)
    assert request.model == GEMINI and not request.tools and request.output is not None
    assert llm.requests == [] and reader.left == 0


async def test_a_gemini_checker_reads_the_passage_and_the_statement_for_a_claude_writer() -> None:
    reader, models = gemini(answered())
    verdict = await support.check(ScriptedLLM(checker=reader), "Hail drove the rise.", [MEMO])
    assert verdict == support.Verdict(supported=True, reason="The memo says so.")
    [call] = models.calls
    [content] = call["contents"]
    assert isinstance(content, types.Content) and content.role == "user" and content.parts is not None
    heading, statement = (part.text or "" for part in content.parts)
    assert heading.startswith(f"Search result 0 (source {MEMO.chunk_id}): {MEMO.title}\n{MEMO.header}\n")
    assert statement == "Statement: Hail drove the rise." and call["model"] == GEMINI


async def test_a_written_answer_is_read_on_the_checker_and_never_by_its_writer() -> None:
    written = reply(*(cited(f" {sentence}", (WORDING.chunk_id, 0)) for sentence in (KEPT, STRETCHED)))
    reader = ScriptedLLM(SUPPORTED, UNSUPPORTED, fast_model=GEMINI, main_model=GEMINI)
    llm = ScriptedLLM(written, written, checker=reader)
    evidence = Evidence((), (WORDING,), (), ())
    done = await writer.write_qual(llm, principal_for("priya"), "Is a burst pipe covered?", evidence)
    assert [request.template for request in llm.requests] == [writer.QUAL_TEMPLATE, writer.QUAL_RETRY_TEMPLATE]
    assert [(request.template, request.model) for request in reader.requests] == [(support.TEMPLATE, GEMINI)] * 2
    assert llm.left == reader.left == 0 and done.retried
    assert done.answer.text == KEPT and [claim.text for claim in done.answer.claims_cut] == [STRETCHED]


async def test_the_live_check_calls_the_checker_too_when_it_is_a_client_of_its_own(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A one-token check always stops at its limit, which the live check counts as an answer.
    cut = reply("", stop_reason="max_tokens")
    missing = Unavailable("live_check.v1: ClientError")
    missing.__cause__ = errors.ClientError(
        404, {"error": {"code": 404, "message": "Publisher Model gemini-9 was not found.", "status": "NOT_FOUND"}}
    )
    readers = [ScriptedLLM(cut, fast_model=GEMINI, main_model=GEMINI), ScriptedLLM(missing, fast_model="gemini-9")]
    clients = iter([ScriptedLLM(cut, cut, checker=reader) for reader in readers])
    monkeypatch.setattr(live_check, "from_env", lambda: next(clients))
    assert await live_check.main() == 0
    assert await live_check.main() == 1
    main = [
        "backend fake",
        "claude-haiku-4-5: OK, answered as claude-haiku-4-5",
        "claude-sonnet-5: OK, answered as claude-sonnet-5",
        "checker fake",
    ]
    assert capsys.readouterr().out.splitlines() == [
        *main,
        f"{GEMINI}: OK, answered as {GEMINI}",
        *main,
        "gemini-9: ClientError (404): Publisher Model gemini-9 was not found.",
    ]
    assert [[request.template for request in reader.requests] for reader in readers] == [["live_check.v1"]] * 2
