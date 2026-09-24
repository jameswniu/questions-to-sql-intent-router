from datetime import date
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import TupleRow

from app.ingest import pipeline
from app.seed import notes
from app.seed.notes import Note
from tests.docs.requires import needs_models

Connection = psycopg.Connection[TupleRow]


def _note(doc_id: str, claim_id: int, body: str) -> Note:
    return Note(
        doc_id=doc_id, claim_id=claim_id, region="North", written=date(2026, 1, 5), author="Test Adjuster",
        body=body, pii=(), injection=None,
    )  # fmt: skip


def test_a_manifest_listed_note_missing_from_disk_raises(tmp_path: Path) -> None:
    notes.write_notes([_note("note-1-1", 1, "Body one."), _note("note-1-2", 1, "Body two.")], tmp_path)
    (tmp_path / "note-1-1.md").unlink()
    with pytest.raises(ValueError, match="missing manifest-listed"):
        pipeline.sources(notes_dir=tmp_path)


def test_an_unmanaged_note_makes_write_notes_raise_and_survives_on_disk(tmp_path: Path) -> None:
    stray = tmp_path / "hand-placed.md"
    stray.write_text("a note nobody generated\n")
    with pytest.raises(ValueError, match="does not own"):
        notes.write_notes([_note("note-1-1", 1, "Body one.")], tmp_path)
    assert stray.read_text() == "a note nobody generated\n"
    assert not (tmp_path / "note-1-1.md").exists()
    assert not (tmp_path / notes.MANIFEST_NAME).exists()


@pytest.mark.integration
def test_empty_notes_dir_raises_and_deletes_nothing(superuser: Connection, tmp_path: Path) -> None:
    before = superuser.execute("SELECT count(*) FROM rag.documents").fetchone()
    with pytest.raises(ValueError, match="refusing to treat"), superuser.transaction(force_rollback=True):
        pipeline.ingest(superuser, notes_dir=tmp_path)
    after = superuser.execute("SELECT count(*) FROM rag.documents").fetchone()
    assert after == before


def _loaded_test_docs(conn: Connection) -> set[str]:
    rows = conn.execute("SELECT doc_id FROM rag.documents WHERE doc_id LIKE 'zz-manifest-test-%'").fetchall()
    return {row[0] for row in rows}


@pytest.mark.integration
@needs_models
def test_a_note_dropped_from_the_generators_output_is_removed_from_disk_and_the_index(
    superuser: Connection, tmp_path: Path
) -> None:
    claims = notes.claims_from_database(superuser)
    kept = _note("zz-manifest-test-kept", claims[0].claim_id, "Kept note for the manifest guard test.")
    dropped = _note("zz-manifest-test-dropped", claims[1].claim_id, "Dropped note for the manifest guard test.")
    with superuser.transaction(force_rollback=True):
        notes.write_notes([kept, dropped], tmp_path)
        pipeline.ingest(superuser, notes_dir=tmp_path)
        assert _loaded_test_docs(superuser) == {kept.doc_id, dropped.doc_id}

        notes.write_notes([kept], tmp_path)
        assert not (tmp_path / "zz-manifest-test-dropped.md").exists()

        pipeline.ingest(superuser, notes_dir=tmp_path)
        assert _loaded_test_docs(superuser) == {kept.doc_id}
