import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from functools import cache
from typing import Literal

from app import db
from app.answer.format import long_date
from app.answer.lookup import LookupResult, answer_lookup
from app.answer.mentions import CLAIM_ID, claim_id_in, date_in, edition_in
from app.answer.passages import as_lines, by_relevance, choose, score
from app.answer.quant import verify_sql
from app.answer.types import Claim, Draft, Evidence
from app.config import settings
from app.identity import Principal
from app.ingest import embed
from app.semantic.layer import default_layer
from app.sources.documents import Hit, search

Basis = Literal["given", "question", "claim", "edition", "today"]
NOT_FOUND = "I couldn't find that in the documents you can see."
NOTES = re.compile(r"\b(?:notes?|noted|file\s+notes?)\b", re.IGNORECASE)
KIND_WORDS = {"bulletin": "bulletin", "memo": "memo", "guideline": "guideline", "guidelines": "guideline"}
# Wide enough that filtering out notes and scans still leaves the candidates to compose from.
SEARCH_K = 12
CANDIDATES = 8
KIND_BOOST = 2.0
# The named claim's row, read as the asker: its loss date is the reading date, and its peril and region are what
# a wording question about it can leave out.
LOSS_DATE_SQL = "SELECT loss_date, peril, region FROM sem.v_claims WHERE claim_id = %s LIMIT 1"
EDITION_SQL = "SELECT min(effective_from) FROM rag.documents WHERE kind = 'wording' AND edition = %s"


@dataclass(frozen=True)
class ReadingDate:
    """The day the policy wording is read at, and what in the question set it."""

    day: date
    basis: Basis
    source: str


@dataclass(frozen=True)
class ClaimFacts:
    """What the wording needs from a claim the asker can read: when the loss happened, its peril and its region."""

    claim_id: int
    loss_date: date
    peril: str
    region: str


@dataclass(frozen=True)
class QualResult:
    kind: Literal["answer", "not_found", "not_allowed"]
    text: str
    draft: Draft
    evidence: Evidence
    hits: tuple[Hit, ...]
    reading: ReadingDate
    claim_id: int | None = None


async def _claim_facts(principal: Principal, question: str) -> ClaimFacts | None:
    """The row of the claim the question names, read as the asker. Analysts have no claim rows to read, and a
    claim outside the user's regions is simply not there."""
    claim_id = claim_id_in(question)
    if claim_id is None or principal.kind == "analyst":
        return None
    sql = verify_sql(LOSS_DATE_SQL, relations=frozenset({"sem.v_claims"}))
    found = await db.run(principal, sql, (claim_id,))
    if not found.rows:
        return None
    loss_date, peril, region = found.rows[0]
    return ClaimFacts(claim_id, loss_date, peril, region)


async def _edition_start(principal: Principal, edition: str) -> date | None:
    found = await db.run(principal, EDITION_SQL, (edition,))
    return found.rows[0][0] if found.rows else None


async def reading_date(principal: Principal, question: str, on_date: date | None = None) -> ReadingDate:
    """An explicit date wins, then the loss date of a claim the user can see, then a named edition, then today."""
    return await _reading_date(principal, question, on_date, await _claim_facts(principal, question))


async def _reading_date(
    principal: Principal, question: str, on_date: date | None, claim: ClaimFacts | None
) -> ReadingDate:
    if on_date is not None:
        return ReadingDate(on_date, "given", long_date(on_date))
    if (day := date_in(question)) is not None:
        return ReadingDate(day, "question", long_date(day))
    if claim is not None:
        return ReadingDate(claim.loss_date, "claim", f"claim {claim.claim_id}")
    if (edition := edition_in(question)) is not None and (start := await _edition_start(principal, edition)):
        return ReadingDate(start, "edition", edition)
    return ReadingDate(settings().as_of, "today", "")


async def _unopened(principal: Principal, claim_id: int) -> LookupResult | None:
    """Lookup's own reply for a claim the asker can't read, or None if lookup opens it after all. A wording question
    about a claim they can't see reads exactly as asking for the claim does, so it says neither more nor less."""
    found = await answer_lookup(principal, claim_id)
    return None if found.kind == "found" else found


@cache
def _names_a_peril() -> re.Pattern[str]:
    """Any word the semantic layer has for a peril (wind, windstorm, burst pipe), matched as the figures path
    matches them."""
    peril = default_layer().dimensions["peril"]
    words = sorted({*peril.values, *peril.synonyms}, key=len, reverse=True)
    either = "|".join(r"[\s-]+".join(re.escape(part) for part in word.split()) for word in words)
    return re.compile(rf"(?<!\w)(?:{either})(?:e?s)?(?!\w)", re.IGNORECASE)


def _topic(question: str, claim: ClaimFacts | None, notes: bool) -> str:
    """What to search the documents for. A claim's number only says which claim, so it is no word to find, except
    in a question asking for the claim's notes, which carry it. A question that names no peril is about the
    claim's own, so its peril and region are words to find: the wording differs by both, as the HO-2025 2% wind
    and hail deductible applies in the West and South only."""
    if notes or claim_id_in(question) is None:
        return question
    topic = CLAIM_ID.sub(" ", question)
    if claim is not None and _names_a_peril().search(question) is None:
        topic = f"{topic} {claim.peril} {claim.region}"
    return topic


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
    claim = await _claim_facts(principal, question)
    reading = await _reading_date(principal, question, on_date, claim)
    claim_id = claim_id_in(question)
    # A named claim the asker can't read gets lookup's own reply, before anything is searched.
    if claim_id is not None and claim is None and (refused := await _unopened(principal, claim_id)):
        kind: Literal["not_found", "not_allowed"] = "not_allowed" if refused.kind == "not_allowed" else "not_found"
        empty = Evidence(rows=(), hits=(), sandbox=(), scan_fields=())
        return QualResult(kind, refused.text, Draft((), ()), empty, (), reading, claim_id)
    # A claim's own notes are read when the question asks for notes. Otherwise the claim says when, by its loss
    # date, and, when the question doesn't, what and where, by its peril and region.
    notes = NOTES.search(question) is not None
    topic = _topic(question, claim, notes)
    found = await search(principal, topic, k=SEARCH_K, as_of_date=reading.day)
    hits = [hit for hit in found if _usable(hit, notes=notes, claim_id=claim_id)][:CANDIDATES]
    if reading.basis != "today" and not notes and any(hit.kind == "wording" for hit in hits):
        # The date or edition the question named decides which form applies; a memo about a later change doesn't.
        hits = [hit for hit in hits if hit.kind == "wording"]
    scored = await asyncio.to_thread(score, hits, topic, rerank=embed.rerank, boost=_boost(question))
    ordered = tuple(by_relevance(scored, hits))
    evidence = Evidence(rows=(), hits=ordered, sandbox=(), scan_fields=())
    chosen = choose(scored, lists=True)
    if not chosen:
        return QualResult("not_found", NOT_FOUND, Draft((), ()), evidence, ordered, reading, claim_id)
    lines = as_lines(chosen)
    claims = tuple(Claim(line, (), (p.hit.chunk_id,)) for p, line in zip(chosen, lines, strict=True))
    draft = Draft(claims, _caveats(reading, [p.hit for p in chosen]))
    text = ("\n" if any(line.startswith("- ") for line in lines) else " ").join(lines)
    return QualResult("answer", text, draft, evidence, ordered, reading, claim_id)
