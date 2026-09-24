import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app import db
from app.answer.format import long_date
from app.answer.mentions import claim_id_in, date_in, edition_in
from app.answer.passages import by_relevance, choose, score
from app.answer.quant import verify_sql
from app.answer.types import Claim, Draft, Evidence
from app.config import settings
from app.identity import Principal
from app.ingest import embed
from app.sources.documents import Hit, search

Basis = Literal["given", "question", "claim", "edition", "today"]
NOT_FOUND = "I couldn't find that in the documents you can see."
NOTES = re.compile(r"\b(?:notes?|noted|file\s+notes?)\b", re.IGNORECASE)
KIND_WORDS = {"bulletin": "bulletin", "memo": "memo", "guideline": "guideline", "guidelines": "guideline"}
# Wide enough that filtering out notes and scans still leaves the candidates to compose from.
SEARCH_K = 12
CANDIDATES = 8
KIND_BOOST = 2.0
LOSS_DATE_SQL = "SELECT loss_date FROM sem.v_claims WHERE claim_id = %s LIMIT 1"
EDITION_SQL = "SELECT min(effective_from) FROM rag.documents WHERE kind = 'wording' AND edition = %s"


@dataclass(frozen=True)
class ReadingDate:
    """The day the policy wording is read at, and what in the question set it."""

    day: date
    basis: Basis
    source: str


@dataclass(frozen=True)
class QualResult:
    kind: Literal["answer", "not_found"]
    text: str
    draft: Draft
    evidence: Evidence
    hits: tuple[Hit, ...]
    reading: ReadingDate
    claim_id: int | None = None


async def _loss_date(principal: Principal, claim_id: int) -> date | None:
    sql = verify_sql(LOSS_DATE_SQL, relations=frozenset({"sem.v_claims"}))
    found = await db.run(principal, sql, (claim_id,))
    return found.rows[0][0] if found.rows else None


async def _edition_start(principal: Principal, edition: str) -> date | None:
    found = await db.run(principal, EDITION_SQL, (edition,))
    return found.rows[0][0] if found.rows else None


async def reading_date(principal: Principal, question: str, on_date: date | None = None) -> ReadingDate:
    """An explicit date wins, then the loss date of a claim the user can see, then a named edition, then today."""
    if on_date is not None:
        return ReadingDate(on_date, "given", long_date(on_date))
    if (day := date_in(question)) is not None:
        return ReadingDate(day, "question", long_date(day))
    claim_id = claim_id_in(question)
    # Analysts have no claim rows to read, and a claim outside the user's regions is simply not there.
    if claim_id is not None and principal.kind != "analyst" and (loss := await _loss_date(principal, claim_id)):
        return ReadingDate(loss, "claim", f"claim {claim_id}")
    if (edition := edition_in(question)) is not None and (start := await _edition_start(principal, edition)):
        return ReadingDate(start, "edition", edition)
    return ReadingDate(settings().as_of, "today", "")


def _usable(hit: Hit, *, notes: bool, claim_id: int | None) -> bool:
    if hit.quarantined or hit.kind == "scan" or (hit.kind == "note" and not notes):
        return False
    # A note about another claim would answer a question about this one with the wrong file.
    return claim_id is None or hit.claim_id in (None, claim_id)


def _boost(question: str) -> Callable[[Hit], float]:
    named = {KIND_WORDS[word] for word in re.findall(r"[a-z]+", question.lower()) if word in KIND_WORDS}
    return lambda hit: KIND_BOOST if hit.kind in named else 0.0


def _caveats(reading: ReadingDate, cited: Sequence[Hit]) -> tuple[str, ...]:
    editions = sorted({hit.edition for hit in cited if hit.edition})
    if not editions or reading.basis in ("today", "edition"):
        return ()
    when = f"on {reading.source}"
    if reading.basis == "claim":
        when = f"on the loss date of {reading.source}, {long_date(reading.day)}"
    return (f"Read against {' and '.join(editions)}, the form in force {when}.",)


async def answer_qual(principal: Principal, question: str, *, on_date: date | None = None) -> QualResult:
    reading = await reading_date(principal, question, on_date)
    claim_id = claim_id_in(question)
    notes = NOTES.search(question) is not None or claim_id is not None
    found = await search(principal, question, k=SEARCH_K, as_of_date=reading.day)
    hits = [hit for hit in found if _usable(hit, notes=notes, claim_id=claim_id)][:CANDIDATES]
    if reading.basis != "today" and not notes and any(hit.kind == "wording" for hit in hits):
        # The date or edition the question named decides which form applies; a memo about a later change doesn't.
        hits = [hit for hit in hits if hit.kind == "wording"]
    scored = await asyncio.to_thread(score, hits, question, rerank=embed.rerank, boost=_boost(question))
    ordered = tuple(by_relevance(scored, hits))
    evidence = Evidence(rows=(), hits=ordered, sandbox=(), scan_fields=())
    chosen = choose(scored)
    if not chosen:
        return QualResult("not_found", NOT_FOUND, Draft((), ()), evidence, ordered, reading, claim_id)
    claims = tuple(Claim(p.text, (), (p.hit.chunk_id,)) for p in chosen)
    draft = Draft(claims, _caveats(reading, [p.hit for p in chosen]))
    return QualResult("answer", " ".join(c.text for c in claims), draft, evidence, ordered, reading, claim_id)
