from dataclasses import dataclass
from typing import Any, Literal

from app.answer.types import Answer

EvidenceKind = Literal["sql", "rows", "chunks", "scan", "sandbox"]
Outcome = Literal["answer", "not_found", "not_allowed", "clarify", "out_of_data", "refused", "timeout", "unavailable"]


@dataclass(frozen=True)
class Stage:
    name: str
    ms: float


@dataclass(frozen=True)
class Evidence:
    kind: EvidenceKind
    payload: Any


@dataclass(frozen=True)
class Refused:
    reason: str
    message: str


@dataclass(frozen=True)
class Clarify:
    question: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class OutOfData:
    message: str
    covered: str


@dataclass(frozen=True)
class Error:
    stage: str
    message: str


@dataclass(frozen=True)
class Live:
    """How live mode went for a request that used it: why it fell back to the no-key answer, if it did, and whether
    the model wrote its draft once more with the verifier's reasons. The request log reads it; the browser never
    gets it."""

    fallback: str | None
    retried: bool = False


@dataclass(frozen=True)
class Done:
    request_id: str
    total_ms: float
    route: str
    outcome: Outcome
    # What the request read, for the audit log.
    claim_ids: tuple[int, ...] = ()
    doc_ids: tuple[str, ...] = ()


Event = Stage | Evidence | Answer | Refused | Clarify | OutOfData | Error | Live | Done
