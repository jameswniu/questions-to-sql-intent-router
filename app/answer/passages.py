import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from app.sources.documents import Hit

_STOPWORD_TEXT = """
    a about after all also am an and any are as at be been before being but by can could did do does doing for
    from had has have how i if in into is it its may me might must my no not of on or our shall should so some such
    tell than that the their them then there these they this those to under up us was we were what when where which
    while who whom why will with would you your say says said show please happen happened happens policy form
    much many
"""
STOPWORDS = frozenset(_STOPWORD_TEXT.split())
WORD = re.compile(r"[a-z]+|\d+(?:[.,]\d+)*")
BULLET = re.compile(r"^(?:[-*]|\d+[.)])\s+")
# A memo's address block (To, From, Date, Re) holds nothing a question could be asking about.
MEMO_HEADER = re.compile(r"^(?:to|from|date|re|cc|subject):\s", re.IGNORECASE)
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9$(\"])")
SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
# A sentence that ends in a colon, or points at "the following", opens the list after it.
ANNOUNCES_LIST = re.compile(r":$|\bthe\s+following\b", re.IGNORECASE)
ASKS_AMOUNT = re.compile(
    r"\b(?:how\s+(?:much|long|soon|many\s+days)|sublimit|limit|deductible|percent(?:age)?|amount|within)\b",
    re.IGNORECASE,
)
STATES_AMOUNT = re.compile(
    r"\$\d|\d\s*%|\b(?:\d+|one|two|three|five|ten|thirty|sixty|ninety)\s+(?:business\s+)?(?:days?|months?|years?)\b",
    re.IGNORECASE,
)
MIN_STEM = 4
# A passage's second and later shared words count half, so one rare word (flood) outweighs two common ones
# (covered, damage).
LATER_WORDS = 0.5
AMOUNT_BONUS = 1.5
# Below this logit on every passage, what came back is about something else. Under zero it takes the question
# having a third of its words nowhere in the candidates to say so.
NOT_RELEVANT = -9.0
MISSING_SHARE = 1 / 3
# Passages from the best chunk join the answer when they score within this of its best passage; a passage from
# another chunk only when it is nearly as good, so an answer stays with one section unless two really tie. A
# table row lists one alternative among many, so its neighbours join only when they tie.
SAME_CHUNK_MARGIN = 2.0
OTHER_CHUNK_MARGIN = 0.75
ROW_MARGIN = 0.5

Rerank = Callable[[str, Sequence[str]], Sequence[float]]
PieceKind = Literal["text", "item", "row"]


@dataclass(frozen=True)
class Passage:
    text: str
    hit: Hit
    position: int
    weight: float
    score: float
    model: float | None = None
    row: bool = False
    item: bool = False


@dataclass(frozen=True)
class Scored:
    passages: tuple[Passage, ...]
    wanted: frozenset[str]
    # Question words that no candidate passage contains.
    missing: frozenset[str]


def terms(text: str) -> frozenset[str]:
    words = (word.replace(",", "") for word in WORD.findall(text.lower()))
    return frozenset(_singular(word) for word in words if word not in STOPWORDS and (len(word) > 2 or word.isdigit()))


def _singular(word: str) -> str:
    if word.endswith("sses"):
        return word[:-2]
    if len(word) > 4 and word.endswith("ies"):
        return f"{word[:-3]}y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def matches(term: str, others: frozenset[str]) -> bool:
    """Exact for numbers. A word also meets its longer forms (roof and roofing, cover and covered) and a word
    sharing a long stem with it (escalate and escalation), but not a lookalike such as flood and floor."""
    if term in others:
        return True
    if not term.isalpha() or len(term) < MIN_STEM:
        return False
    for other in others:
        if other.isalpha() and len(other) >= MIN_STEM:
            if other.startswith(term) or term.startswith(other):
                return True
            if len(os.path.commonprefix([term, other])) >= max(6, min(len(term), len(other)) - 2):
                return True
    return False


def _closed(text: str) -> str:
    return text if text.endswith((".", "!", "?", ":")) else f"{text}."


def pieces(body: str) -> list[tuple[str, bool]]:
    """Sentences, list items and table rows, each readable on its own, flagged when a table row.
    A row keeps its column names, so it still reads right away from its header."""
    return [(text, kind == "row") for text, kind in _pieces(body)]


def _pieces(body: str) -> list[tuple[str, PieceKind]]:
    """The pieces of pieces(), each with its kind: a sentence of prose, a whole list item, or a table row."""
    out: list[tuple[str, PieceKind]] = []
    lines: list[str] = []
    item = False
    header: list[str] | None = None

    def flush() -> None:
        if lines:
            text = " ".join(lines)
            out.extend([(_closed(text), "item")] if item else [(s, "text") for s in SENTENCE_BREAK.split(text) if s])
            lines.clear()

    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("|"):
            flush()
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if all(SEPARATOR_CELL.match(cell) for cell in cells):
                continue
            if header is None:
                header = cells
            else:
                row = "; ".join(f"{name}: {cell}" for name, cell in zip(header, cells, strict=False))
                out.append((_closed(row), "row"))
            continue
        header = None
        if not line or MEMO_HEADER.match(line):
            flush()
        elif BULLET.match(line):
            flush()
            item = True
            lines.append(BULLET.sub("", line, count=1))
        else:
            if item and not raw.startswith((" ", "\t")):
                flush()
                item = False
            lines.append(line)
    flush()
    return out


def split(body: str) -> list[str]:
    return [text for text, _ in pieces(body)]


def _softplus(x: float) -> float:
    return x + math.log1p(math.exp(-x)) if x > 0 else math.log1p(math.exp(x))


def _saturating(weights: list[float]) -> float:
    ordered = sorted(weights, reverse=True)
    return ordered[0] + LATER_WORDS * sum(ordered[1:]) if ordered else 0.0


def score(
    hits: Sequence[Hit],
    query: str,
    *,
    context: str = "",
    rerank: Rerank | None = None,
    boost: Callable[[Hit], float] | None = None,
) -> Scored:
    """Every passage of the hits, scored against the query.

    A shared word counts for more the fewer passages carry it. The cross-encoder, when given, reads each passage
    under its section heading, which is often where the word the question used lives; its logit enters through
    softplus, so a confident read counts in full and a rejection adds nothing and leaves the words to decide.
    Words in context count like the query's own but are not shown to the cross-encoder.
    """
    wanted = terms(query) | terms(context)
    parts = [(hit, position, text, kind) for hit in hits for position, (text, kind) in enumerate(_pieces(hit.body))]
    if not wanted or not parts:
        return Scored((), wanted, wanted)
    found = [terms(text) for _, _, text, _ in parts]
    headings = {hit.chunk_id: terms(hit.header or hit.title) for hit in hits}
    counts = {term: sum(matches(term, words) for words in found) for term in wanted}
    idf = {term: math.log((len(parts) + 1) / (count + 0.5)) if count else 0.0 for term, count in counts.items()}
    logits: list[float | None] = [None] * len(parts)
    if rerank:
        logits = list(rerank(query, [f"{hit.header or hit.title}\n{text}" for hit, _, text, _ in parts]))
    amount = ASKS_AMOUNT.search(query) is not None
    passages = []
    for (hit, position, text, kind), words, logit in zip(parts, found, logits, strict=True):
        weight = _saturating([idf[t] for t in wanted if matches(t, words)])
        section = _saturating([idf[t] for t in wanted if matches(t, headings[hit.chunk_id])]) / 2
        extra = _softplus(logit) if logit is not None else 0.0
        extra += AMOUNT_BONUS if amount and STATES_AMOUNT.search(text) else 0.0
        extra += boost(hit) if boost else 0.0
        total = weight + section + extra
        passages.append(Passage(text, hit, position, weight, total, logit, kind == "row", kind == "item"))
    missing = frozenset(term for term, count in counts.items() if not count)
    return Scored(tuple(passages), wanted, missing)


def by_relevance(scored: Scored, hits: Sequence[Hit]) -> list[Hit]:
    """Hits ordered by their best passage, any without a scored passage last in their original order."""
    best: dict[str, float] = {}
    for p in scored.passages:
        best[p.hit.chunk_id] = max(best.get(p.hit.chunk_id, p.score), p.score)
    return sorted(hits, key=lambda hit: -best.get(hit.chunk_id, -math.inf))


def _unanswered(scored: Scored) -> bool:
    logits = [p.model for p in scored.passages if p.model is not None]
    if logits and max(logits) < NOT_RELEVANT:
        return True
    unsure = not logits or max(logits) < 0
    return unsure and len(scored.missing) >= MISSING_SHARE * len(scored.wanted)


def _opens_list(passage: Passage) -> bool:
    return ANNOUNCES_LIST.search(passage.text) is not None


def _listed(scored: Scored, chosen: Sequence[Passage]) -> list[Passage]:
    """The items of each list a chosen passage opens: the items right after it in its chunk, up to the first
    passage that isn't one."""
    at = {(p.hit.chunk_id, p.position): p for p in scored.passages}
    items = []
    for opener in (p for p in chosen if _opens_list(p)):
        position = opener.position + 1
        while (following := at.get((opener.hit.chunk_id, position))) is not None and following.item:
            items.append(following)
            position += 1
    return items


def choose(
    scored: Scored,
    *,
    most: int = 4,
    least: int = 2,
    other_margin: float = OTHER_CHUNK_MARGIN,
    judge: bool = True,
    lists: bool = False,
) -> list[Passage]:
    """Two to four passages around the best one, in reading order, or none when nothing is about the question.

    judge=False skips that last check, for a caller that has already decided the hits are the right ones.
    lists=True lets a chosen passage that opens a list ("when any of the following applies") bring the whole
    list: the items are what it says, though they may share no word with the question, and one item picked out
    by its words would read as the only one.
    """
    relevant = [p for p in scored.passages if p.weight > 0]
    if not relevant or (judge and _unanswered(scored)):
        return []
    ranked = sorted(relevant, key=lambda p: (-p.score, p.position))
    best = ranked[0]
    chosen = [best]
    for passage in ranked[1:]:
        if len(chosen) == most:
            break
        margin = other_margin
        if passage.hit.chunk_id == best.hit.chunk_id:
            margin = ROW_MARGIN if passage.row or best.row else SAME_CHUNK_MARGIN
        if passage.score >= best.score - margin:
            chosen.append(passage)
    if lists:
        chosen += [item for item in dict.fromkeys(_listed(scored, chosen)) if item not in chosen]
    if _sentences(chosen) < least and not best.row:
        # A lone short passage reads as a fragment; its neighbour in the same chunk supplies the context.
        neighbours = [p for p in scored.passages if p.hit.chunk_id == best.hit.chunk_id and p not in chosen]
        neighbours.sort(key=lambda p: (abs(p.position - best.position), p.position < best.position))
        chosen += neighbours[: least - _sentences(chosen)]
    first_seen: dict[str, int] = {}
    for i, p in enumerate(chosen):
        first_seen.setdefault(p.hit.chunk_id, i)
    return sorted(chosen, key=lambda p: (first_seen[p.hit.chunk_id], p.position))


def as_lines(chosen: Sequence[Passage]) -> list[str]:
    """The chosen passages as an answer says them: the items of a list read as bullets under the sentence that
    opens it, and everything else as written."""
    opened = {p.hit.chunk_id for p in chosen if _opens_list(p)}
    return [f"- {p.text}" if p.item and p.hit.chunk_id in opened else p.text for p in chosen]


def _sentences(passages: Sequence[Passage]) -> int:
    return sum(len(SENTENCE_BREAK.split(p.text)) for p in passages)
