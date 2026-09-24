from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from app.llm.client import LLMError
from app.llm.facts import IsolationViolation


class Invalid(Exception):
    """A model's output, or a tool call it made, that doesn't hold up. The live path falls back on it."""

    reason = "invalid"


class SandboxError(Exception):
    """The sandbox couldn't run an analysis."""

    reason = "sandbox"


class OverBudget(Exception):
    """The live why path used its steps, its time, or the tool calls it may make."""

    def __init__(self, reason: Literal["steps", "time", "calls"]) -> None:
        super().__init__(reason)
        self.reason = reason


class Unread(Exception):
    """A live-written draft whose reading didn't run, or none of whose written sentences held."""

    reason = "reading"


@dataclass(frozen=True)
class Fallback:
    """A live step that didn't hold, and why, so the no-key answer stands and the request log says so."""

    reason: str


@dataclass
class Notes:
    """What live mode noted during one request, held by the pipeline, so a fallback is still logged when the no-key
    answer that replaces it fails in turn."""

    fallback: str | None = None


NOTES: ContextVar[Notes | None] = ContextVar("live_notes", default=None)


def fell_back(reason: str) -> None:
    """Notes, before the no-key answer runs, that a live step fell back and why."""
    notes = NOTES.get()
    if notes is not None and notes.fallback is None:
        notes.fallback = reason


def reason_for(exc: BaseException) -> str:
    """The short reason a live step fell back, as the request log and the traces record it."""
    if isinstance(exc, LLMError | Invalid | SandboxError | OverBudget | Unread):
        return exc.reason
    if isinstance(exc, IsolationViolation):
        return "isolation"
    if isinstance(exc, TimeoutError):
        return "time"
    return "error"
