import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import TupleRow
from tokenizers import Tokenizer

from app.config import settings
from app.ingest import embed, pipeline
from app.seed import notes
from app.seed.notes import INJECTIONS_FIXTURE
from tests.docs.requires import needs_models

pytestmark = pytest.mark.integration

Connection = psycopg.Connection[TupleRow]
KEY_FIELDS = ("claim_number", "vendor", "date", "total")


@needs_models
def test_a_second_ingest_changes_nothing(superuser: Connection, tmp_path: Path) -> None:
    notes.write_notes(notes.generate(notes.claims_from_database(superuser)), tmp_path)
    with superuser.transaction(force_rollback=True):
        report = pipeline.ingest(superuser, notes_dir=tmp_path)
    assert (report.new, report.changed, report.removed) == (0, 0, 0), report.summary()
    assert (report.unchanged,) == superuser.execute("SELECT count(*) FROM rag.documents").fetchone()


@needs_models
def test_every_document_records_the_pinned_embedding_model(superuser: Connection) -> None:
    models = superuser.execute("SELECT DISTINCT embed_model FROM rag.documents").fetchall()
    assert models == [(embed.model_id(),)]
    assert embed.model_id().startswith("Qdrant/bge-small-en-v1.5-onnx-Q@") and "unknown" not in embed.model_id()


@needs_models
def test_every_chunk_fits_the_embedders_window_with_its_header(superuser: Connection) -> None:
    path = settings().embed_model_path
    assert path is not None
    tokenizer = Tokenizer.from_file(str(path / "tokenizer.json"))
    tokenizer.no_truncation()
    texts = [f"{h}\n{b}" for h, b in superuser.execute("SELECT header, body FROM rag.chunks")]
    longest = max(len(e.ids) for e in tokenizer.encode_batch(texts))
    assert longest <= 512, longest


def test_region_and_sensitivity_come_from_the_owning_claim(superuser: Connection) -> None:
    disagree = superuser.execute(
        """
        SELECT d.doc_id FROM rag.documents d JOIN core.claims c ON c.claim_id = d.claim_id WHERE d.region <> c.region
        UNION ALL
        SELECT ch.chunk_id FROM rag.chunks ch JOIN rag.documents d ON d.doc_id = ch.doc_id
        WHERE (ch.region, ch.claim_id, ch.sensitivity) IS DISTINCT FROM (d.region, d.claim_id, d.sensitivity)
        """
    ).fetchall()
    assert disagree == []
    kinds = superuser.execute(
        "SELECT kind, sensitivity, count(*) FROM rag.documents GROUP BY 1, 2 ORDER BY 1"
    ).fetchall()
    assert {kind: sensitivity for kind, sensitivity, _ in kinds} == {
        "bulletin": "general", "guideline": "general", "memo": "general", "note": "claim", "scan": "claim",
        "wording": "general",
    }  # fmt: skip


def test_only_policy_wordings_carry_an_edition(superuser: Connection) -> None:
    rows = superuser.execute(
        "SELECT d.kind, c.edition, c.effective_from, c.effective_to, count(*)"
        " FROM rag.chunks c JOIN rag.documents d ON d.doc_id = c.doc_id GROUP BY 1, 2, 3, 4 ORDER BY 1, 2"
    ).fetchall()
    for kind, edition, start, _, _ in rows:
        assert (edition is not None) == (kind == "wording") and (start is not None) == (kind == "wording"), kind


def test_exactly_the_planted_injections_are_quarantined(superuser: Connection) -> None:
    planted = {row["doc_id"] for row in json.loads(INJECTIONS_FIXTURE.read_text())}
    quarantined = {row[0] for row in superuser.execute("SELECT doc_id FROM rag.chunks WHERE quarantined")}
    assert quarantined == planted


def test_no_policyholder_identifier_reaches_a_chunk(superuser: Connection) -> None:
    leaked = superuser.execute(
        """
        SELECT ch.chunk_id FROM rag.chunks ch
        JOIN core.claims c ON c.claim_id = ch.claim_id
        JOIN core.policies p ON p.policy_id = c.policy_id
        JOIN core.policyholders h ON h.policyholder_id = p.policyholder_id
        WHERE strpos(ch.body, h.ssn) > 0 OR strpos(ch.body, h.email) > 0 OR strpos(ch.body, h.phone) > 0
           OR strpos(ch.body, h.first_name || ' ' || h.last_name) > 0 OR strpos(ch.body, replace(h.ssn, '-', '')) > 0
        """
    ).fetchall()
    assert leaked == []
    masked = superuser.execute(
        "SELECT count(*) FROM rag.chunks WHERE body ~ '\\[(SSN|DOB|PHONE|EMAIL|NAME)\\]'"
    ).fetchone()
    assert masked is not None and masked[0] > 100


def _scan_fields(superuser: Connection) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for doc_id, field, value, flagged in superuser.execute("SELECT doc_id, field, value, flagged FROM rag.scan_fields"):
        out.setdefault(doc_id, {})[field] = (value, flagged)
    return out


def test_scan_flags_catch_every_planted_mismatch_and_every_misread_key_field(
    superuser: Connection, truth: list[dict[str, Any]]
) -> None:
    fields = _scan_fields(superuser)
    assert len(fields) == len(truth)
    for row in truth:
        read = fields[row["doc_id"]]
        expected = {"claim_number": row["claim_number"], "vendor": row["vendor"], "date": row["date"]}
        expected["total"] = row["total"]
        for name in KEY_FIELDS:
            value, flagged = read.get(name, (None, True))
            wrong = value != expected[name] and not (name == "total" and value == f"${expected[name]}")
            assert flagged or not wrong, (row["doc_id"], name, value, expected[name])
        if row["mismatch"]:
            assert read["total"][1], row["doc_id"]
        if not row["mismatch"] and not row["degraded"]:
            assert not any(flagged for _, flagged in read.values()), row["doc_id"]
            assert Decimal(read["total"][0]) == Decimal(row["ledger_total"])
