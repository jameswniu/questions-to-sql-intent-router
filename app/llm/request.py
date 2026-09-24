import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.llm.facts import Fact, IsolationViolation, check, to_json

Effort = Literal["low", "medium", "high"]
# The shortest prefix each model will cache, from the prompt caching docs. A shorter prefix is silently not
# cached, so it is not marked. A model missing here is never marked.
CACHE_MINIMUM_TOKENS = {
    "claude-opus-5": 512,
    "claude-fable-5-1": 512,
    "claude-opus-4-8": 1024,
    "claude-sonnet-5": 1024,
    "claude-sonnet-4-6": 1024,
    "claude-sonnet-4-5": 1024,
    "claude-opus-4-7": 2048,
    "claude-opus-4-6": 4096,
    "claude-haiku-4-5": 4096,
}
# Prose runs about four characters a token or fewer, so this undercounts tokens and never marks a prefix too short.
CHARS_PER_TOKEN = 4
TURN_BLOCKS = frozenset({"text", "tool_use", "thinking", "redacted_thinking"})


@dataclass(frozen=True)
class Passage:
    """Document text, and the only way it enters a request: it is sent as a search_result block named by source."""

    source: str
    title: str
    blocks: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.source or not self.title or not self.blocks or not all(b.strip() for b in self.blocks):
            raise ValueError("a passage needs a source, a title and text in every block")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """What a tool call returned, checked to hold only ids, enums and numbers."""

    tool_use_id: str
    content: Fact
    is_error: bool = False

    def __post_init__(self) -> None:
        check(self.content)


@dataclass(frozen=True)
class Turn:
    """An assistant turn as the model returned it, sent back unchanged so a tool loop can go on."""

    blocks: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        for block in self.blocks:
            if block.get("type") not in TURN_BLOCKS or "citations" in block:
                raise IsolationViolation(f"an echoed turn can't carry a {block.get('type')!r} block or citations")


type Part = str | Passage | ToolResult | Turn


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    parts: tuple[Part, ...]

    def __post_init__(self) -> None:
        allowed = (str, Turn) if self.role == "assistant" else (str, Passage, ToolResult)
        for part in self.parts:
            if not isinstance(part, allowed):
                raise ValueError(f"a {self.role} message can't carry a {type(part).__name__}")


@dataclass(frozen=True)
class Output:
    """A structured output: the reply must be one JSON object that fits the schema."""

    name: str
    schema: Mapping[str, Any]


@dataclass(frozen=True)
class Request:
    """One model call. Building it enforces the isolation rule, so a request that breaks it never exists."""

    template: str
    model: str
    system: tuple[str, ...]
    messages: tuple[Message, ...]
    max_tokens: int
    tools: tuple[Tool, ...] = ()
    output: Output | None = None
    cache: bool = False
    effort: Effort | None = None
    timeout_s: float = 20.0

    def __post_init__(self) -> None:
        if not self.messages or self.messages[0].role != "user":
            raise ValueError("a request starts with a user message")
        if self.tools and self.passages:
            raise IsolationViolation(f"{self.template}: a request that offers tools can't carry document text")

    def _parts(self) -> Iterator[Part]:
        for message in self.messages:
            yield from message.parts

    @property
    def passages(self) -> tuple[Passage, ...]:
        """The request's passages in the order the API numbers search results, which is how citations name them."""
        return tuple(part for part in self._parts() if isinstance(part, Passage))

    @property
    def cached(self) -> bool:
        """Whether the prefix is marked for caching: only when asked, and only when it is long enough to cache."""
        minimum = CACHE_MINIMUM_TOKENS.get(self.model.partition("@")[0])
        if not self.cache or minimum is None:
            return False
        prefix = sum(map(len, self.system)) + len(json.dumps(self._tools(), sort_keys=True))
        return prefix // CHARS_PER_TOKEN >= minimum

    @property
    def prompt_hash(self) -> str:
        """Identifies the prompt: its template, system text and tool schemas. Nothing a user wrote goes into it."""
        prompt = {"template": self.template, "system": list(self.system), "tools": self._tools()}
        return hashlib.sha256(json.dumps(prompt, sort_keys=True).encode()).hexdigest()

    def _tools(self) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "input_schema": dict(t.input_schema), "strict": True}
            for t in self.tools
        ]

    def _part(self, part: Part) -> list[dict[str, Any]]:
        if isinstance(part, str):
            return [{"type": "text", "text": part}]
        if isinstance(part, Passage):
            # Citations can't be combined with a structured output, and must be on for every result or none.
            return [
                {
                    "type": "search_result",
                    "source": part.source,
                    "title": part.title,
                    "content": [{"type": "text", "text": block} for block in part.blocks],
                    "citations": {"enabled": self.output is None},
                }
            ]
        if isinstance(part, ToolResult):
            result: dict[str, Any] = {"type": "tool_result", "tool_use_id": part.tool_use_id}
            result["content"] = to_json(part.content)
            if part.is_error:
                result["is_error"] = True
            return [result]
        return [dict(block) for block in part.blocks]

    def params(self) -> dict[str, Any]:
        """The request body as the Messages API takes it. There are no sampling parameters, ever."""
        system: list[dict[str, Any]] = [{"type": "text", "text": text} for text in self.system]
        if self.cached:
            system[-1]["cache_control"] = {"type": "ephemeral"}
        messages = [
            {"role": m.role, "content": [block for part in m.parts for block in self._part(part)]}
            for m in self.messages
        ]
        body: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens, "messages": messages}
        if system:
            body["system"] = system
        if self.tools:
            body["tools"] = self._tools()
        config: dict[str, Any] = {}
        if self.output is not None:
            config["format"] = {"type": "json_schema", "schema": dict(self.output.schema)}
        if self.effort is not None:
            config["effort"] = self.effort
        if config:
            body["output_config"] = config
        return body
