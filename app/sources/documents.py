import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Literal

from psycopg import sql

from app import db
from app.config import settings
from app.identity import Principal
from app.ingest import embed

# lexical and vector rank by one signal each; rrf fuses them; hybrid fuses and then reranks.
Mode = Literal["lexical", "vector", "rrf", "hybrid"]
KINDS = frozenset({"wording", "guideline", "memo", "bulletin", "note", "scan"})
CANDIDATES = 30
RRF_K = 60
RERANK_TOP = 20


@dataclass(frozen=True)
class Hit:
    chunk_id: str
    doc_id: str
    anchor: str
    title: str
    header: str | None
    body: str
    kind: str
    score: float
    lexical_rank: int | None
    vector_rank: int | None
    quarantined: bool
    edition: str | None
    region: str | None = None
    claim_id: int | None = None


# Visibility is row-level security's job; these filters only narrow what the caller asked for. A wording
# chunk is kept when its edition was in force on the date, so an old loss is read against the old form.
FILTERS = sql.SQL(
    """(%(include_quarantined)s OR NOT c.quarantined)
      AND (%(kinds)s::text[] IS NULL OR d.kind = ANY (%(kinds)s::text[]))
      AND (c.edition IS NULL
           OR (c.effective_from <= %(on)s AND (c.effective_to IS NULL OR %(on)s <= c.effective_to)))"""
)
# Any stemmed term may match, and ts_rank_cd favours chunks that match more of them. Requiring every term, as
# websearch_to_tsquery does, leaves a natural question with no lexical candidates at all.
LEXICAL = sql.SQL(
    """lexical AS (
    SELECT c.chunk_id, ts_rank_cd(c.tsv, q.tsq) AS score,
           row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q.tsq) DESC, c.chunk_id) AS rank
    FROM rag.chunks c
    JOIN rag.documents d ON d.doc_id = c.doc_id
    CROSS JOIN (SELECT replace(plainto_tsquery('english', %(query)s)::text, '&', '|')::tsquery AS tsq) q
    WHERE c.tsv @@ q.tsq AND {filters}
    ORDER BY rank
    LIMIT %(candidates)s
)"""
).format(filters=FILTERS)
VECTOR = sql.SQL(
    """vector AS (
    SELECT c.chunk_id, 1 - (c.embedding <=> %(vector)s::vector) AS score,
           row_number() OVER (ORDER BY c.embedding <=> %(vector)s::vector, c.chunk_id) AS rank
    FROM rag.chunks c
    JOIN rag.documents d ON d.doc_id = c.doc_id
    WHERE c.embedding IS NOT NULL AND {filters}
    ORDER BY rank
    LIMIT %(candidates)s
)"""
).format(filters=FILTERS)
EMPTY = {
    "lexical": sql.SQL(
        "lexical AS (SELECT NULL::text AS chunk_id, NULL::real AS score, NULL::bigint AS rank WHERE false)"
    ),
    "vector": sql.SQL(
        "vector AS (SELECT NULL::text AS chunk_id, NULL::float8 AS score, NULL::bigint AS rank WHERE false)"
    ),
}
SELECT = sql.SQL(
    """
SELECT c.chunk_id, c.doc_id, c.anchor, d.title, c.header, c.body, d.kind, c.quarantined, c.edition, c.region,
       c.claim_id, l.rank, l.score, v.rank, v.score
FROM rag.chunks c
JOIN rag.documents d ON d.doc_id = c.doc_id
LEFT JOIN lexical l ON l.chunk_id = c.chunk_id
LEFT JOIN vector v ON v.chunk_id = c.chunk_id
WHERE l.chunk_id IS NOT NULL OR v.chunk_id IS NOT NULL"""
)


def statement(mode: Mode) -> sql.Composed:
    lexical = LEXICAL if mode != "vector" else EMPTY["lexical"]
    vector = VECTOR if mode != "lexical" else EMPTY["vector"]
    return sql.SQL("WITH {}, {}").format(lexical, vector) + SELECT


def _rrf(lexical_rank: int | None, vector_rank: int | None) -> float:
    return sum(1 / (RRF_K + rank) for rank in (lexical_rank, vector_rank) if rank is not None)


async def search(
    principal: Principal,
    query: str,
    *,
    k: int = 6,
    kinds: Sequence[str] | None = None,
    as_of_date: date | None = None,
    include_quarantined: bool = False,
    mode: Mode = "hybrid",
) -> list[Hit]:
    """Top chunks for a question, read as the principal so row-level security decides what can come back."""
    if kinds is not None and not set(kinds) <= KINDS:
        raise ValueError(f"unknown document kinds: {sorted(set(kinds) - KINDS)}")
    vector = await asyncio.to_thread(embed.query, query) if mode != "lexical" else None
    params: dict[str, Any] = {
        "query": query,
        "vector": embed.literal(vector) if vector is not None else None,
        "kinds": list(kinds) if kinds is not None else None,
        "on": as_of_date or settings().as_of,
        "include_quarantined": include_quarantined,
        "candidates": CANDIDATES,
    }
    result = await db.run(principal, statement(mode), params)
    candidates = []
    for row in result.rows:
        (chunk_id, doc_id, anchor, title, header, body, kind, quarantined, edition, region, claim_id,
         lexical_rank, lexical_score, vector_rank, vector_score) = row  # fmt: skip
        score = {
            "lexical": lexical_score,
            "vector": vector_score,
            "rrf": _rrf(lexical_rank, vector_rank),
            "hybrid": _rrf(lexical_rank, vector_rank),
        }[mode]
        candidates.append(
            Hit(chunk_id, doc_id, anchor, title, header, body, kind, float(score), lexical_rank, vector_rank,
                quarantined, edition, region, claim_id)
        )  # fmt: skip
    candidates.sort(key=lambda hit: (-hit.score, hit.chunk_id))
    if mode != "hybrid":
        return candidates[:k]
    top = candidates[: max(RERANK_TOP, k)]
    scores = await asyncio.to_thread(embed.rerank, query, [f"{h.header}\n{h.body}" for h in top])
    reranked = [replace(hit, score=score) for hit, score in zip(top, scores, strict=True)]
    reranked.sort(key=lambda hit: (-hit.score, hit.chunk_id))
    return reranked[:k]
