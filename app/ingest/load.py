from collections.abc import Sequence
from typing import TYPE_CHECKING

import psycopg
from psycopg.rows import TupleRow

from app.ingest.embed import literal

if TYPE_CHECKING:
    from app.ingest.pipeline import Prepared

Connection = psycopg.Connection[TupleRow]


def loaded(conn: Connection) -> dict[str, tuple[str, str | None]]:
    """doc_id to (sha256, embed_model) for everything already in rag.documents."""
    return {row[0]: (row[1], row[2]) for row in conn.execute("SELECT doc_id, sha256, embed_model FROM rag.documents")}


def replace(
    conn: Connection,
    prepared: Sequence["Prepared"],
    removed: Sequence[str],
    vectors: Sequence[Sequence[float]],
    model: str,
    regions: dict[int, str],
) -> None:
    """Deletes the removed and changed documents, then inserts the prepared ones. Chunks and fields cascade.

    Region and sensitivity come from the owning claim, never from where the file sits.
    """
    conn.execute(
        "DELETE FROM rag.documents WHERE doc_id = ANY (%s)", ([*removed, *(p.source.doc_id for p in prepared)],)
    )
    vector_of = iter(vectors)
    with conn.cursor() as cur:
        for p in prepared:
            claim_id = p.source.claim_id
            region = regions[claim_id] if claim_id is not None else None
            sensitivity = "claim" if claim_id is not None else "general"
            cur.execute(
                "INSERT INTO rag.documents (doc_id, kind, title, ref, edition, effective_from, effective_to, region,"
                " claim_id, sensitivity, uri, sha256, embed_model)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    p.source.doc_id, p.source.kind, p.title, p.ref, p.edition, p.effective_from, p.effective_to,
                    region, claim_id, sensitivity, p.source.uri, p.source.sha256, model,
                ),
            )  # fmt: skip
            cur.executemany(
                "INSERT INTO rag.chunks (chunk_id, doc_id, anchor, section_path, ordinal, header, body, embedding,"
                " region, sensitivity, claim_id, edition, effective_from, effective_to, quarantined, quarantine_reason)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        c.chunk_id, p.source.doc_id, c.anchor, c.section_path, c.ordinal, c.header, c.body,
                        literal(next(vector_of)), region, sensitivity, claim_id, p.edition, p.effective_from,
                        p.effective_to, c.quarantined, c.quarantine_reason,
                    )
                    for c in p.chunks
                ],
            )  # fmt: skip
            cur.executemany(
                "INSERT INTO rag.scan_fields (doc_id, field, value, confidence, bbox, flagged, flag_reason, region,"
                " sensitivity, claim_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (p.source.doc_id, f.name, f.value, f.confidence, f.bbox, f.flagged, f.flag_reason, region,
                     sensitivity, claim_id)
                    for f in p.fields
                ],
            )  # fmt: skip
    if next(vector_of, None) is not None:
        raise ValueError("more vectors than chunks")
