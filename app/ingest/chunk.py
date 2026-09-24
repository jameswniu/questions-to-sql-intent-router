import re
from dataclasses import dataclass

from app.ingest.parse import Block, Section

# The embedder truncates at 512 tokens, and the contextual header rides along with every chunk.
MAX_TOKENS = 350

# Words and punctuation marks, counted separately. It tracks a WordPiece count more closely than
# whitespace words do, because "$8,280.00" is six tokens to the model, not one.
TOKEN = re.compile(r"\w+|[^\w\s]")
ABBREVIATIONS = ("No.", "Co.", "Inc.", "St.", "Mr.", "Ms.", "Mrs.", "Dr.", "e.g.", "i.e.", "approx.", "D.O.B.")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"(])")


@dataclass(frozen=True)
class Chunk:
    anchor: str
    section_path: str
    # Order of the chunk within its document, like a page number.
    ordinal: int
    body: str


def approx_tokens(text: str) -> int:
    return len(TOKEN.findall(text))


def sentences(text: str) -> list[str]:
    parts: list[str] = []
    for piece in SENTENCE_END.split(text):
        if parts and parts[-1].endswith(ABBREVIATIONS):
            parts[-1] += " " + piece
        else:
            parts.append(piece)
    return parts


def _table_groups(block: Block, limit: int) -> list[str]:
    header, rule, rows = block.lines[0], block.lines[1], block.lines[2:]
    groups: list[list[str]] = []
    for row in rows:
        if groups and approx_tokens("\n".join([header, rule, *groups[-1], row])) <= limit:
            groups[-1].append(row)
        else:
            groups.append([row])
    return ["\n".join([header, rule, *group]) for group in groups]


def _units(block: Block, limit: int) -> list[str]:
    """Pieces no larger than the limit where possible. A sentence is never cut, even if it runs long."""
    if approx_tokens(block.text) <= limit:
        return [block.text]
    if block.kind == "table":
        return _table_groups(block, limit)
    units: list[str] = []
    for line in block.lines:
        if approx_tokens(line) <= limit:
            units.append(line)
            continue
        current = ""
        for sentence in sentences(line):
            joined = f"{current} {sentence}".strip()
            if current and approx_tokens(joined) > limit:
                units.append(current)
                current = sentence
            else:
                current = joined
        units.append(current)
    return units


def chunk_section(section: Section, limit: int = MAX_TOKENS) -> list[str]:
    bodies: list[str] = []
    previous: Block | None = None
    for block in section.blocks:
        for unit in _units(block, limit):
            # Items of a list that had to be split go back together one per line.
            glue = "\n" if block is previous and block.kind == "list" else "\n\n"
            if bodies and approx_tokens(bodies[-1] + glue + unit) <= limit:
                bodies[-1] += glue + unit
            else:
                bodies.append(unit)
            previous = block
    return bodies


def chunk(sections: list[Section], limit: int = MAX_TOKENS) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section in sections:
        for body in chunk_section(section, limit):
            chunks.append(Chunk(section.anchor, " > ".join(section.path), len(chunks) + 1, body))
    return chunks
