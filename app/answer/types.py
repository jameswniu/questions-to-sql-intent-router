from dataclasses import dataclass
from typing import Any

from app.answer.format import NumberRef
from app.sources.documents import Hit


@dataclass(frozen=True)
class Claim:
    text: str
    numbers: tuple[NumberRef, ...]
    # chunk_ids of the passages the sentence rests on
    citations: tuple[str, ...]


@dataclass(frozen=True)
class Draft:
    claims: tuple[Claim, ...]
    caveats: tuple[str, ...]


@dataclass(frozen=True)
class Evidence:
    rows: tuple[dict[str, Any], ...]
    hits: tuple[Hit, ...]
    sandbox: tuple[dict[str, Any], ...]
    scan_fields: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Answer:
    text: str
    claims_kept: tuple[Claim, ...]
    claims_cut: tuple[Claim, ...]
    could_not_confirm: tuple[str, ...]
    citations: tuple[Hit, ...]
