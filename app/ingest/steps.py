import psycopg
from psycopg.rows import TupleRow

from app.config import settings
from app.ingest import pipeline
from app.seed import documents, notes

Connection = psycopg.Connection[TupleRow]


def render_documents(conn: Connection) -> str:
    """The committed corpus must be what policy.yaml and events.yaml render to, or answers cite stale terms."""
    rendered = documents.corpus()
    stale = documents.stale_files(rendered)
    if stale:
        raise RuntimeError(f"data/corpus is out of date ({', '.join(stale)}); run python -m app.seed.documents")
    return f"{len(rendered)} documents, data/corpus current"


def render_notes(conn: Connection) -> str:
    rendered = notes.generate(notes.claims_from_database(conn), settings().as_of)
    written = notes.write_notes(rendered)
    return f"{written} notes over {len({n.claim_id for n in rendered})} claims in {notes.NOTES_DIR}"


def ingest(conn: Connection) -> str:
    return pipeline.ingest(conn).summary()
