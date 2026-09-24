import csv
import io
import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

# The one threshold for trusting what tesseract read. Clean scans in this corpus read at 90 and above;
# blur and low contrast pull whole fields well below it.
MIN_CONFIDENCE = 80.0

CURRENCY = re.compile(r"^\$?(\d{1,3}(?:,\d{3})*|\d+)\.\d{2}$")
CLAIM_NUMBER = re.compile(r"^\d{6}$")

# Label words per field, in the order they are tried. A value is the text to the right of its label on the
# same line, which is how these forms are laid out.
LABELS: dict[str, list[tuple[str, ...]]] = {
    "claim_number": [("claim", "no")],
    "date": [("invoice", "date"), ("estimate", "date"), ("date", "signed")],
    "vendor": [("remit", "to"), ("contractor",)],
    "total": [("total", "due"), ("estimate", "total"), ("net", "amount", "claimed")],
}
ITEMS_HEADER = ("description", "amount")


@dataclass(frozen=True)
class Word:
    text: str
    conf: float
    left: int
    top: int
    width: int
    height: int
    line: tuple[int, int, int]


@dataclass
class Field:
    name: str
    value: str | None
    confidence: float | None
    bbox: list[int] | None
    flags: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return bool(self.flags)

    @property
    def flag_reason(self) -> str | None:
        return ", ".join(self.flags) or None


def tesseract(path: Path) -> list[Word]:
    # One thread per process; the caller runs several scans at once instead.
    env = os.environ | {"OMP_THREAD_LIMIT": "1"}
    out = subprocess.run(
        ["tesseract", str(path), "stdout", "--psm", "6", "tsv"], capture_output=True, check=True, env=env, timeout=60
    ).stdout.decode()
    return parse_tsv(out)


def parse_tsv(tsv: str) -> list[Word]:
    words = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (row.get("text") or "").strip()
        if row["level"] != "5" or not text:
            continue
        words.append(
            Word(
                text=text,
                conf=float(row["conf"]),
                left=int(row["left"]),
                top=int(row["top"]),
                width=int(row["width"]),
                height=int(row["height"]),
                line=(int(row["block_num"]), int(row["par_num"]), int(row["line_num"])),
            )
        )
    return words


def lines(words: Sequence[Word]) -> list[list[Word]]:
    grouped: dict[tuple[int, int, int], list[Word]] = {}
    for word in words:
        grouped.setdefault(word.line, []).append(word)
    ordered = sorted(grouped.values(), key=lambda ws: (min(w.top for w in ws), min(w.left for w in ws)))
    return [sorted(ws, key=lambda w: w.left) for ws in ordered]


def text_of(words: Sequence[Word]) -> str:
    return "\n".join(" ".join(w.text for w in line) for line in lines(words))


def _key(word: Word) -> str:
    return re.sub(r"[^a-z0-9]", "", word.text.lower())


def _find(line: list[Word], label: tuple[str, ...]) -> int | None:
    """Index just past the label in the line, if the line contains it."""
    keys = [_key(w) for w in line]
    for i in range(len(keys) - len(label) + 1):
        if tuple(keys[i : i + len(label)]) == label:
            return i + len(label)
    return None


def _field(name: str, words: list[Word]) -> Field:
    if not words:
        return Field(name, None, None, None, ["missing"])
    left, top = min(w.left for w in words), min(w.top for w in words)
    right, bottom = max(w.left + w.width for w in words), max(w.top + w.height for w in words)
    confidence = sum(w.conf for w in words) / len(words)
    return Field(name, " ".join(w.text for w in words), round(confidence, 1), [left, top, right - left, bottom - top])


def extract(words: Sequence[Word]) -> dict[str, Field]:
    """Fields by label proximity. Values are as read; check() normalizes them and raises flags."""
    rows = lines(words)
    found: dict[str, Field] = {}
    total_row: int | None = None
    for name, labels in LABELS.items():
        for label in labels:
            hit = next(((i, end) for i, line in enumerate(rows) if (end := _find(line, label)) is not None), None)
            if hit is None:
                continue
            i, end = hit
            value = rows[i][end:]
            if name == "vendor":
                cut = next((j for j, w in enumerate(value) if w.text.endswith(",")), None)
                if cut is not None:
                    value = value[: cut + 1]
            found[name] = _field(name, value)
            if name == "total":
                total_row = i
            break
        else:
            found[name] = _field(name, [])

    header = next((i for i, line in enumerate(rows) if _find(line, ITEMS_HEADER) is not None), None)
    if header is not None and total_row is not None:
        for n, line in enumerate(rows[header + 1 : total_row], 1):
            if len(line) < 2:
                continue
            item = _field(f"line_{n}", line)
            description, amount = " ".join(w.text for w in line[:-1]), line[-1].text
            item.value = f"{description} | {amount}"
            found[item.name] = item
    return found


def amount(text: str | None) -> Decimal | None:
    if text is None or not CURRENCY.match(text):
        return None
    return Decimal(text.lstrip("$").replace(",", ""))


def check(fields: dict[str, Field], claim_id: int, ledger_total: Decimal) -> dict[str, Field]:
    """Normalize values in place and flag low confidence, bad formats and disagreement with the claim."""
    for f in fields.values():
        if f.value is None:
            continue
        if f.confidence is not None and f.confidence < MIN_CONFIDENCE:
            f.flags.append("low confidence")
        if f.name == "claim_number":
            f.value = f.value.replace(" ", "")
            if not CLAIM_NUMBER.match(f.value):
                f.flags.append("format")
            elif int(f.value) != claim_id:
                f.flags.append("does not match the claim")
        elif f.name == "date":
            try:
                f.value = datetime.strptime(f.value.replace(" ", ""), "%m/%d/%Y").date().isoformat()
            except ValueError:
                f.flags.append("format")
        elif f.name == "vendor":
            f.value = f.value.rstrip(",")
        elif f.name == "total" or f.name.startswith("line_"):
            description, _, raw = f.value.rpartition(" | ") if f.name != "total" else ("", "", f.value)
            parsed = amount(raw.replace(" ", "") if f.name == "total" else raw)
            if parsed is None:
                f.flags.append("format")
                continue
            f.value = f"{description} | {parsed:.2f}" if description else f"{parsed:.2f}"
            if f.name == "total" and parsed != ledger_total:
                f.flags.append("does not match payments")
    total = amount_of(fields.get("total"))
    items = [amount_of(f) for name, f in fields.items() if name.startswith("line_")]
    readable = [item for item in items if item is not None]
    if total is not None and items and len(readable) == len(items) and sum(readable, Decimal(0)) != total:
        fields["total"].flags.append("line items do not add up")
    return fields


def amount_of(f: Field | None) -> Decimal | None:
    if f is None or f.value is None or "format" in f.flags:
        return None
    try:
        return Decimal(f.value.rpartition(" | ")[2])
    except ArithmeticError:
        return None
