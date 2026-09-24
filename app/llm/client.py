import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import cache
from typing import Any, Protocol

import anthropic
import pydantic
from anthropic.types import (
    CitationsSearchResultLocation,
    Message,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from google.auth.exceptions import GoogleAuthError

from app import telemetry
from app.config import backend_from
from app.llm.request import Output, Request

log = logging.getLogger(__name__)

FAST_MODEL = "claude-haiku-4-5"
MAIN_MODEL = "claude-sonnet-5"
# The only region where these models answered; us-east5 and europe-west1 gave 404s.
DEFAULT_REGION = "global"
MAX_RETRIES = 2


class LLMError(Exception):
    """A live model call that failed. Every caller answers from the no-key path instead."""

    reason = "model"


class Refused(LLMError):
    reason = "refusal"


class Truncated(LLMError):
    reason = "max_tokens"


class Unavailable(LLMError):
    reason = "unavailable"


class InvalidOutput(LLMError):
    reason = "invalid_output"


@dataclass(frozen=True)
class Citation:
    """Where a passage of the answer comes from: the search result's source and index, and the blocks it cites."""

    source: str
    index: int
    start: int
    end: int
    cited_text: str


@dataclass(frozen=True)
class Text:
    text: str
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: Mapping[str, Any]


@dataclass(frozen=True)
class Usage:
    """Token counts as the API reports them: input_tokens leaves out what was read from or written to the cache."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    # None when the model has no list price in the table, so an unknown cost never reads as zero.
    cost_usd: Decimal | None = None


@dataclass(frozen=True)
class Response:
    """A reply in this app's own terms. Nothing outside this module sees the SDK's objects."""

    texts: tuple[Text, ...]
    tool_calls: tuple[ToolCall, ...]
    stop_reason: str
    model: str
    usage: Usage
    latency_ms: float
    prompt_hash: str
    # The assistant turn as the API takes it back, thinking blocks included, for the next step of a tool loop.
    turn: tuple[Mapping[str, Any], ...]

    @property
    def text(self) -> str:
        return "".join(t.text for t in self.texts)


class LLM(Protocol):
    @property
    def fast_model(self) -> str: ...

    @property
    def main_model(self) -> str: ...

    @property
    def provider(self) -> str: ...

    async def complete(self, request: Request) -> Response:
        """Sends the request and returns the reply whatever its stop reason. Callers go through ask."""
        ...


def cost(model: str, usage: Usage) -> Decimal | None:
    price = telemetry.PRICES.get(model.partition("@")[0])
    if price is None:
        return None
    rate_in, rate_out, rate_read = price
    written = usage.cache_write * rate_in * telemetry.CACHE_WRITE_RATE
    dollars = usage.input_tokens * rate_in + written + usage.cache_read * rate_read + usage.output_tokens * rate_out
    return dollars / 1_000_000


async def ask(llm: LLM, request: Request) -> Response:
    """One model call under a GenAI span. A refusal or a reply cut off by its token limit raises, so no caller
    reads a partial answer."""
    with telemetry.model_call(request.model, provider=llm.provider) as call:
        call.record_template(request.template, request.prompt_hash)
        response = await llm.complete(request)
        usage = response.usage
        call.record_usage(
            usage.input_tokens,
            usage.output_tokens,
            cache_read=usage.cache_read,
            cache_write=usage.cache_write,
            response_model=response.model,
            finish_reason=response.stop_reason,
        )
    if response.stop_reason == "refusal":
        raise Refused(f"{request.template}: the model declined")
    if response.stop_reason in ("max_tokens", "model_context_window_exceeded"):
        raise Truncated(f"{request.template}: the reply was cut off at {request.max_tokens} tokens")
    if response.stop_reason not in ("end_turn", "tool_use", "stop_sequence"):
        raise LLMError(f"{request.template}: the reply stopped for {response.stop_reason}")
    return response


def parse[M: pydantic.BaseModel](response: Response, model: type[M]) -> M:
    """The reply's structured output as the model class, or InvalidOutput."""
    try:
        return model.model_validate_json(response.text)
    except pydantic.ValidationError as exc:
        raise InvalidOutput(f"the reply isn't a valid {model.__name__}: {exc.error_count()} problems") from exc


def load(response: Response) -> Any:
    """The reply's structured output as plain JSON, for a schema the caller checks itself."""
    try:
        return json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise InvalidOutput("the reply isn't JSON") from exc


def output_for(model: type[pydantic.BaseModel]) -> Output:
    """A structured output whose schema is the model class's, shaped by the SDK the way messages.parse does."""
    return Output(model.__name__, anthropic.transform_schema(model))


def _citations(block: TextBlock) -> tuple[Citation, ...]:
    return tuple(
        Citation(c.source, c.search_result_index, c.start_block_index, c.end_block_index, c.cited_text)
        for c in block.citations or ()
        if isinstance(c, CitationsSearchResultLocation)
    )


def normalize(message: Message, request: Request, latency_ms: float) -> Response:
    """The reply in this module's terms. A block of a type this app never asks for, or one the SDK couldn't read,
    raises, so it is never echoed back into a tool loop."""
    texts: list[Text] = []
    calls: list[ToolCall] = []
    turn: list[dict[str, Any]] = []
    for block in message.content:
        if block.type == "text" and isinstance(block, TextBlock) and isinstance(block.text, str):
            texts.append(Text(block.text, _citations(block)))
            turn.append({"type": "text", "text": block.text})
        elif block.type == "tool_use" and isinstance(block, ToolUseBlock) and isinstance(block.input, dict):
            calls.append(ToolCall(block.id, block.name, dict(block.input)))
            turn.append({"type": "tool_use", "id": block.id, "name": block.name, "input": dict(block.input)})
        elif block.type == "thinking" and isinstance(block, ThinkingBlock):
            turn.append({"type": "thinking", "thinking": block.thinking, "signature": block.signature})
        elif block.type == "redacted_thinking" and isinstance(block, RedactedThinkingBlock):
            turn.append({"type": "redacted_thinking", "data": block.data})
        else:
            raise Unavailable(f"{request.template}: the reply held a {block.type!r} block")
    raw = message.usage
    usage = Usage(
        raw.input_tokens, raw.output_tokens, raw.cache_read_input_tokens or 0, raw.cache_creation_input_tokens or 0
    )
    return Response(
        tuple(texts),
        tuple(calls),
        message.stop_reason or "end_turn",
        message.model,
        replace(usage, cost_usd=cost(message.model, usage)),
        round(latency_ms, 1),
        request.prompt_hash,
        tuple(turn),
    )


class AnthropicLLM:
    """Claude through the Anthropic API or Vertex AI. Both clients share the Messages API, so one class serves."""

    def __init__(
        self,
        client: anthropic.AsyncAnthropic | anthropic.AsyncAnthropicVertex,
        *,
        fast_model: str,
        main_model: str,
        provider: str,
    ) -> None:
        self._client = client
        self._fast, self._main, self._provider = fast_model, main_model, provider

    @property
    def fast_model(self) -> str:
        return self._fast

    @property
    def main_model(self) -> str:
        return self._main

    @property
    def provider(self) -> str:
        return self._provider

    async def complete(self, request: Request) -> Response:
        started = time.perf_counter()
        try:
            message = await self._client.messages.create(**request.params(), timeout=request.timeout_s)
            if not isinstance(message, Message):
                raise TypeError(f"the API answered with a {type(message).__name__}, not a message")
            return normalize(message, request, (time.perf_counter() - started) * 1000)
        except Unavailable:
            raise
        # A malformed body reaches here as a decoding, type or attribute error rather than an API error.
        except (anthropic.AnthropicError, GoogleAuthError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
            raise Unavailable(f"{request.template}: {type(exc).__name__}") from exc


class LiveConfigError(ValueError):
    pass


def from_env(env: Mapping[str, str] | None = None) -> LLM | None:
    """The live model client the environment asks for, or None when LLM_BACKEND is unset or off, the default."""
    env = os.environ if env is None else env
    try:
        backend = backend_from(env)
    except ValueError as exc:
        raise LiveConfigError(str(exc)) from None
    if backend == "none":
        return None
    models = {
        "fast_model": env.get("LIVE_FAST_MODEL") or FAST_MODEL,
        "main_model": env.get("LIVE_MAIN_MODEL") or MAIN_MODEL,
    }
    client: anthropic.AsyncAnthropic | anthropic.AsyncAnthropicVertex
    if backend == "anthropic":
        key = env.get("ANTHROPIC_API_KEY")
        if not key:
            raise LiveConfigError("LLM_BACKEND=anthropic needs ANTHROPIC_API_KEY")
        client = anthropic.AsyncAnthropic(api_key=key, max_retries=MAX_RETRIES)
    else:
        project = env.get("VERTEX_PROJECT_ID")
        if not project:
            raise LiveConfigError("LLM_BACKEND=vertex needs VERTEX_PROJECT_ID")
        region = env.get("VERTEX_REGION") or DEFAULT_REGION
        client = anthropic.AsyncAnthropicVertex(project_id=project, region=region, max_retries=MAX_RETRIES)
    return AnthropicLLM(client, provider=telemetry.PROVIDERS[backend], **models)


@cache
def default() -> LLM | None:
    """The process's live client, built on first use so that importing the app never constructs one."""
    llm = from_env()
    if llm is not None:
        log.info("live mode on: %s, fast model %s, main model %s", llm.provider, llm.fast_model, llm.main_model)
    return llm
