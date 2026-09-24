import hashlib
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import TupleRow

from app.config import DATA_DIR, ROOT
from app.ingest import embed, load, mask, ocr, screen
from app.ingest.chunk import MAX_TOKENS, chunk
from app.ingest.parse import Block, Section, parse
from app.seed import notes

# Part of every document's hash, so a change to how documents are chunked, masked or screened re-ingests
# everything once. Bump it with any such change.
PIPELINE = "ingest-2"
CORPUS_DIR = DATA_DIR / "corpus"
SCANS_DIR = DATA_DIR / "scans"
SCAN_NAME = re.compile(r"^(\d+)-(invoice|estimate|proof-of-loss)\.png$")
SCAN_TITLES = {"invoice": "Repair invoice", "estimate": "Contractor estimate", "proof-of-loss": "Proof of loss"}

Connection = psycopg.Connection[TupleRow]


@dataclass(frozen=True)
class Source:
    doc_id: str
    kind: str
    path: Path
    uri: str
    sha256: str
    claim_id: int | None
    # Markdown documents carry their text; a scan is read by OCR only if it has to be ingested.
    text: str | None


@dataclass(frozen=True)
class Claim:
    claim_id: int
    region: str
    holder: str
    paid_indemnity: Decimal


@dataclass
class PreparedChunk:
    chunk_id: str
    anchor: str
    section_path: str
    ordinal: int
    header: str
    body: str
    quarantined: bool
    quarantine_reason: str | None


@dataclass
class Prepared:
    source: Source
    title: str
    ref: str | None = None
    edition: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    chunks: list[PreparedChunk] = field(default_factory=list)
    fields: list[ocr.Field] = field(default_factory=list)
    masked: Counter[str] = field(default_factory=Counter)


@dataclass
class Report:
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    removed: int = 0
    chunks: Counter[str] = field(default_factory=Counter)
    quarantined: int = 0
    masked: Counter[str] = field(default_factory=Counter)
    flagged_fields: int = 0
    seconds: dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        chunks = ", ".join(f"{kind} {n}" for kind, n in sorted(self.chunks.items())) or "none"
        timing = ", ".join(f"{stage} {s:.1f}s" for stage, s in self.seconds.items())
        return (
            f"{self.new} new, {self.changed} changed, {self.removed} removed, {self.unchanged} unchanged;"
            f" chunks {chunks}; {self.quarantined} quarantined; {self.flagged_fields} scan fields flagged; {timing}"
        )


def _digest(data: bytes) -> str:
    return hashlib.sha256(PIPELINE.encode() + b"\0" + data).hexdigest()


def _markdown(path: Path, uri: str) -> Source:
    text = path.read_text()
    meta = parse(text).meta
    claim_id = meta.get("claim_id")
    return Source(str(meta["doc_id"]), str(meta["kind"]), path, uri, _digest(text.encode()), claim_id, text)


def sources(notes_dir: Path = notes.NOTES_DIR) -> list[Source]:
    if not notes_dir.is_dir():
        # Ingesting without notes would delete every note already loaded.
        raise FileNotFoundError(f"no notes under {notes_dir}; run the render_notes step first")
    # An empty, partial or tampered notes_dir would otherwise read as "every note was removed".
    notes.verify_manifest(notes_dir)
    found = [_markdown(path, str(path.relative_to(ROOT))) for path in sorted(CORPUS_DIR.rglob("*.md"))]
    found += [_markdown(path, f"notes/{path.name}") for path in sorted(notes_dir.glob("*.md"))]
    for path in sorted(SCANS_DIR.glob("*.png")):
        match = SCAN_NAME.match(path.name)
        if match is None:
            raise ValueError(f"unexpected file in {SCANS_DIR}: {path.name}")
        doc_id = f"scan-{match.group(1)}-{match.group(2)}"
        found.append(
            Source(
                doc_id, "scan", path, str(path.relative_to(ROOT)), _digest(path.read_bytes()), int(match.group(1)), None
            )
        )
    return found


def claims(conn: Connection, claim_ids: set[int]) -> dict[int, Claim]:
    rows = conn.execute(
        """
        SELECT c.claim_id, c.region, h.first_name || ' ' || h.last_name,
               coalesce((SELECT sum(p.amount) FROM core.payments p
                         WHERE p.claim_id = c.claim_id AND p.kind = 'indemnity' AND p.status <> 'voided'), 0)
        FROM core.claims c
        JOIN core.policies pol ON pol.policy_id = c.policy_id
        JOIN core.policyholders h ON h.policyholder_id = pol.policyholder_id
        WHERE c.claim_id = ANY (%s)
        """,
        (sorted(claim_ids),),
    ).fetchall()
    return {row[0]: Claim(*row) for row in rows}


def header(
    title: str, ref: str | None, kind: str, path: tuple[str, ...], edition: str | None, region: str | None
) -> str:
    """The line embedded ahead of each chunk, so a chunk carries the context its body leaves out."""
    parts = [f"{ref} {kind}: {title}" if ref and ref not in title else title]
    if len(path) > 1:
        parts.append(" > ".join(path[1:]))
    if edition:
        parts.append(f"edition {edition}")
    if region:
        parts.append(f"{region} region")
    return " | ".join(parts)


def _chunks(prepared: Prepared, sections: list[Section], region: str | None, doc_text: str) -> list[PreparedChunk]:
    out = []
    parts: Counter[str] = Counter()
    paths = {s.anchor: s.path for s in sections}
    # An instruction worded across a chunk boundary matches no single chunk, so the whole document is
    # screened as well. normalize() keeps no offsets into the original, so a document-level match
    # quarantines every chunk of the document rather than guessing which one carries it.
    doc_verdict = screen.screen(doc_text)
    for c in chunk(sections, MAX_TOKENS):
        parts[c.anchor] += 1
        verdict = screen.screen(c.body)
        if verdict.quarantined:
            reason = verdict.reason
        elif doc_verdict.quarantined:
            reason = f"document-level match spanning a chunk boundary: {doc_verdict.reason}"
        else:
            reason = None
        out.append(
            PreparedChunk(
                chunk_id=f"{c.anchor}:{parts[c.anchor]}",
                anchor=c.anchor,
                section_path=c.section_path,
                ordinal=c.ordinal,
                header=header(
                    prepared.title, prepared.ref, prepared.source.kind, paths[c.anchor], prepared.edition, region
                ),
                body=c.body,
                quarantined=verdict.quarantined or doc_verdict.quarantined,
                quarantine_reason=reason,
            )
        )
    return out


def prepare_markdown(source: Source, claim: Claim | None) -> Prepared:
    assert source.text is not None
    parsed = parse(source.text)
    meta: dict[str, Any] = parsed.meta
    prepared = Prepared(
        source,
        title=str(meta["title"]),
        ref=meta.get("ref"),
        edition=meta.get("edition"),
        effective_from=meta.get("effective_from"),
        effective_to=meta.get("effective_to"),
    )
    sections = parsed.sections
    doc_text = source.text
    if claim is not None:
        masked = mask.mask(source.text, [claim.holder])
        prepared.masked = masked.counts
        sections = parse(masked.text).sections
        doc_text = masked.text
    prepared.chunks = _chunks(prepared, sections, claim.region if claim else None, doc_text)
    return prepared


def prepare_scan(source: Source, claim: Claim, words: list[ocr.Word]) -> Prepared:
    kind = source.doc_id.split("-", 2)[2]
    prepared = Prepared(source, title=f"{SCAN_TITLES[kind]}, claim {claim.claim_id}")
    masked = mask.mask(ocr.text_of(words), [claim.holder])
    prepared.masked = masked.counts
    lines = tuple(line for line in masked.text.splitlines() if line.strip())
    section = Section(f"{source.doc_id}#page-1", prepared.title, (prepared.title,), 1, (Block("text", lines),))
    prepared.chunks = _chunks(prepared, [section], claim.region, masked.text)
    prepared.fields = list(ocr.check(ocr.extract(words), claim.claim_id, claim.paid_indemnity).values())
    return prepared


def ingest(conn: Connection, notes_dir: Path = notes.NOTES_DIR) -> Report:
    """Brings rag.* in line with the files: new and changed documents are loaded, removed ones deleted."""
    report = Report()
    started = time.perf_counter()
    found = sources(notes_dir)
    model = embed.model_id()
    loaded = load.loaded(conn)
    todo = [s for s in found if loaded.get(s.doc_id) != (s.sha256, model)]
    removed = sorted(loaded.keys() - {s.doc_id for s in found})
    report.new = sum(s.doc_id not in loaded for s in todo)
    report.changed = len(todo) - report.new
    report.unchanged = len(found) - len(todo)
    report.removed = len(removed)
    facts = claims(conn, {s.claim_id for s in todo if s.claim_id is not None})
    missing = {s.claim_id for s in todo if s.claim_id is not None} - facts.keys()
    if missing:
        raise ValueError(f"documents name claims that do not exist: {sorted(missing)[:5]}")

    owner = {s.doc_id: facts[s.claim_id] for s in todo if s.claim_id is not None}

    scans = [s for s in todo if s.kind == "scan"]
    mark = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
        words = list(pool.map(lambda s: ocr.tesseract(s.path), scans))
    report.seconds["ocr"] = time.perf_counter() - mark

    prepared = [prepare_markdown(s, owner.get(s.doc_id)) for s in todo if s.kind != "scan"]
    prepared += [prepare_scan(s, owner[s.doc_id], w) for s, w in zip(scans, words, strict=True)]
    texts = [f"{c.header}\n{c.body}" for p in prepared for c in p.chunks]
    mark = time.perf_counter()
    vectors = embed.passages(texts) if texts else []
    report.seconds["embed"] = time.perf_counter() - mark

    mark = time.perf_counter()
    load.replace(conn, prepared, removed, vectors, model, {c.claim_id: c.region for c in facts.values()})
    report.seconds["load"] = time.perf_counter() - mark
    report.seconds["total"] = time.perf_counter() - started
    for p in prepared:
        report.chunks[p.source.kind] += len(p.chunks)
        report.quarantined += sum(c.quarantined for c in p.chunks)
        report.masked += p.masked
        report.flagged_fields += sum(f.flagged for f in p.fields)
    return report
