import itertools
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from app.llm.client import FAST_MODEL, MAIN_MODEL, Citation, Response, Text, ToolCall, Usage
from app.llm.request import Request

type Step = Response | BaseException | Callable[[Request], Response | Awaitable[Response]]

_ids = itertools.count(1)


class ScriptExhausted(BaseException):
    """The code under test made a call the script didn't expect. A BaseException, so no fallback swallows it."""


class ScriptedLLM:
    """Answers with scripted responses in order and records every request it was sent, for tests to assert on."""

    def __init__(self, *script: Step, fast_model: str = FAST_MODEL, main_model: str = MAIN_MODEL) -> None:
        self._script = list(script)
        self._fast, self._main = fast_model, main_model
        self.requests: list[Request] = []

    @property
    def fast_model(self) -> str:
        return self._fast

    @property
    def main_model(self) -> str:
        return self._main

    @property
    def provider(self) -> str:
        return "fake"

    @property
    def left(self) -> int:
        return len(self._script)

    async def complete(self, request: Request) -> Response:
        self.requests.append(request)
        if not self._script:
            raise ScriptExhausted(f"no scripted reply for request {len(self.requests)} ({request.template})")
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        made = step if isinstance(step, Response) else step(request)
        response = made if isinstance(made, Response) else await made
        return replace(response, model=response.model or request.model, prompt_hash=request.prompt_hash)

    def sent(self, template: str) -> list[Request]:
        return [r for r in self.requests if r.template == template]


def reply(*texts: str | Text, tool_calls: Sequence[ToolCall] = (), stop_reason: str | None = None) -> Response:
    blocks = tuple(t if isinstance(t, Text) else Text(t) for t in texts)
    turn: list[Mapping[str, Any]] = [{"type": "text", "text": b.text} for b in blocks]
    turn += [{"type": "tool_use", "id": c.id, "name": c.name, "input": dict(c.input)} for c in tool_calls]
    stop = stop_reason or ("tool_use" if tool_calls else "end_turn")
    return Response(blocks, tuple(tool_calls), stop, "", Usage(), 0.0, "", tuple(turn))


def json_reply(value: object, *, stop_reason: str | None = None) -> Response:
    return reply(json.dumps(value), stop_reason=stop_reason)


def calls(*requested: tuple[str, Mapping[str, Any]]) -> Response:
    """A turn that calls these tools in order, each with a call id of its own."""
    made = [ToolCall(f"toolu_{next(_ids)}", name, dict(arguments)) for name, arguments in requested]
    return reply(tool_calls=made)


def cited(text: str, *sources: tuple[str, int], cited_text: str = "") -> Text:
    """A text block citing search results by (source, search_result_index), each at its first block."""
    return Text(text, tuple(Citation(source, index, 0, 1, cited_text) for source, index in sources))
