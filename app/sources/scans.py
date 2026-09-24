import asyncio
import re
from dataclasses import dataclass

from app import db
from app.config import DATA_DIR, ROOT
from app.identity import Principal

SCANS_DIR = DATA_DIR / "scans"
FIELD_ORDER = {"claim_number": 0, "vendor": 1, "date": 2, "total": 1000}


@dataclass(frozen=True)
class ScanField:
    doc_id: str
    title: str
    field: str
    value: str | None
    confidence: float | None
    bbox: list[int] | None
    flagged: bool
    flag_reason: str | None


def _position(name: str) -> int:
    line = re.fullmatch(r"line_(\d+)", name)
    return 10 + int(line.group(1)) if line else FIELD_ORDER.get(name, 500)


async def scan_fields(principal: Principal, claim_id: int) -> list[ScanField]:
    """Fields read from the claim's scans, empty when the claim is outside the principal's regions."""
    rows = await db.run(
        principal,
        "SELECT f.doc_id, d.title, f.field, f.value, f.confidence, f.bbox, f.flagged, f.flag_reason"
        " FROM rag.scan_fields f JOIN rag.documents d ON d.doc_id = f.doc_id WHERE f.claim_id = %s",
        (claim_id,),
    )
    fields = [ScanField(*row) for row in rows.rows]
    return sorted(fields, key=lambda f: (f.doc_id, _position(f.field)))


async def scan_image(principal: Principal, doc_id: str) -> bytes | None:
    """The page image, but only after the principal can read the document row itself."""
    rows = await db.run(principal, "SELECT uri FROM rag.documents WHERE doc_id = %s AND kind = 'scan'", (doc_id,))
    if not rows.rows:
        return None
    path = (ROOT / rows.rows[0][0]).resolve()
    if not path.is_relative_to(SCANS_DIR.resolve()):
        raise ValueError(f"{doc_id} points outside {SCANS_DIR}")
    return await asyncio.to_thread(path.read_bytes)
