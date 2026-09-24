import asyncio
import itertools
import time
from dataclasses import replace
from typing import Self

import httpx
from google import genai
from google.auth.exceptions import GoogleAuthError
from google.genai import errors, types

from app import telemetry
from app.llm.client import LLM, Response, Text, Unavailable, Usage, cost
from app.llm.request import Passage, Request

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_LOCATION = "global"
# The SDK retries rate limits, server errors and dropped connections itself, backing off between tries.
ATTEMPTS = 4
# Thinking counts against max_output_tokens, so a request that names no effort thinks low and leaves its short limit
# to the reply. Vertex refuses minimal.
THINKING = {"low": types.ThinkingLevel.LOW, "medium": types.ThinkingLevel.MEDIUM, "high": types.ThinkingLevel.HIGH}
ROLES = {"user": "user", "assistant": "model"}
STOPS = {"STOP": "end_turn", "MAX_TOKENS": "max_tokens"}
# The finish reasons of a reply Google's filters stopped, which ask counts as the model declining.
REFUSALS = frozenset({"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"})


def _passage(index: int, passage: Passage) -> types.Part:
    heading = f"Search result {index} (source {passage.source}): {passage.title}"
    return types.Part(text="\n".join((heading, *passage.blocks)))


def contents(request: Request) -> list[types.Content]:
    """The request's messages as Gemini takes them, each passage as text numbered from 0 in request order, the way
    the Messages API numbers search results. Gemini only reads here: a request that offers tools, or whose reply
    should cite its documents, needs Claude's search results, so it is refused before anything is sent."""
    if request.tools:
        raise Unavailable(f"{request.template}: Gemini is never offered tools")
    if request.passages and request.output is None:
        raise Unavailable(f"{request.template}: a reply that cites its documents needs Claude's search results")
    numbers = itertools.count()
    made: list[types.Content] = []
    for message in request.messages:
        parts: list[types.Part] = []
        for part in message.parts:
            if isinstance(part, str):
                parts.append(types.Part(text=part))
            elif isinstance(part, Passage):
                parts.append(_passage(next(numbers), part))
            else:
                raise Unavailable(f"{request.template}: Gemini can't take a {type(part).__name__}")
        made.append(types.Content(role=ROLES[message.role], parts=parts))
    return made


def config(request: Request) -> types.GenerateContentConfig:
    """The call's token limit, thinking level, system text and output schema. There are no sampling parameters, and
    the SDK's automatic function calling is off, since Gemini is never offered tools."""
    shape = request.output
    return types.GenerateContentConfig(
        system_instruction="\n\n".join(request.system) if request.system else None,
        max_output_tokens=request.max_tokens,
        thinking_config=types.ThinkingConfig(thinking_level=THINKING[request.effort or "low"]),
        response_mime_type="application/json" if shape is not None else None,
        response_json_schema=dict(shape.schema) if shape is not None else None,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


def stop_reason(response: types.GenerateContentResponse) -> str:
    """The finish reason in the terms ask reads. A prompt Gemini blocked has no candidate, only the reason it was
    blocked, and counts as a refusal. Any other reason keeps its own name, lowercased, and ask raises for it."""
    if not response.candidates:
        feedback = response.prompt_feedback
        return "refusal" if feedback is not None and feedback.block_reason is not None else "no_candidate"
    reason = response.candidates[0].finish_reason
    name = reason.name if reason is not None else "FINISH_REASON_UNSPECIFIED"
    return "refusal" if name in REFUSALS else STOPS.get(name, name.lower())


def normalize(response: types.GenerateContentResponse, request: Request, latency_ms: float) -> Response:
    """The reply in this app's terms. Gemini counts cached tokens inside the prompt and bills thinking as output, so
    input leaves out the cache read and output takes in the thinking, as Anthropic reports them."""
    counts = response.usage_metadata or types.GenerateContentResponseUsageMetadata()
    cached = counts.cached_content_token_count or 0
    usage = Usage(
        (counts.prompt_token_count or 0) - cached,
        (counts.candidates_token_count or 0) + (counts.thoughts_token_count or 0),
        cached,
    )
    model = response.model_version or request.model
    text = response.text
    return Response(
        (Text(text),) if text else (),
        (),
        stop_reason(response),
        model,
        replace(usage, cost_usd=cost(model, usage)),
        round(latency_ms, 1),
        request.prompt_hash,
        (),
    )


class GeminiLLM:
    """Gemini on Vertex AI, signed in with application default credentials. It checks what Claude wrote, so the
    reading of a cited sentence doesn't come from the writer's own model family."""

    def __init__(self, client: genai.Client, *, model: str = DEFAULT_MODEL) -> None:
        self._client, self._model = client, model

    @classmethod
    def on_vertex(cls, project: str, location: str = DEFAULT_LOCATION, model: str = DEFAULT_MODEL) -> Self:
        retries = types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=ATTEMPTS))
        return cls(genai.Client(vertexai=True, project=project, location=location, http_options=retries), model=model)

    @property
    def fast_model(self) -> str:
        return self._model

    @property
    def main_model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return telemetry.PROVIDERS["vertex"]

    @property
    def checker(self) -> LLM:
        return self

    async def complete(self, request: Request) -> Response:
        # The SDK types contents as a list of its content union, and a list[Content] isn't one.
        made: list[types.ContentUnion] = list(contents(request))
        settings = config(request)
        started = time.perf_counter()
        try:
            async with asyncio.timeout(request.timeout_s):
                response = await self._client.aio.models.generate_content(
                    model=request.model, contents=made, config=settings
                )
        # Once its retries are spent the SDK re-raises a dropped connection as httpx raised it, not as an API error.
        except (errors.APIError, GoogleAuthError, httpx.HTTPError, TimeoutError, OSError, ValueError) as exc:
            raise Unavailable(f"{request.template}: {type(exc).__name__}") from exc
        return normalize(response, request, (time.perf_counter() - started) * 1000)
