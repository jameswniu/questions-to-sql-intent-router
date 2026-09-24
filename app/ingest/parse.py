import re
from dataclasses import dataclass
from typing import Any, Literal, cast

import yaml

BlockKind = Literal["text", "list", "table"]


@dataclass(frozen=True)
class Block:
    kind: BlockKind
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass(frozen=True)
class Section:
    anchor: str
    heading: str
    path: tuple[str, ...]
    # Position of the section in its document, counted from 1, so a citation can say where to look.
    ordinal: int
    blocks: tuple[Block, ...]


@dataclass(frozen=True)
class Parsed:
    meta: dict[str, Any]
    sections: list[Section]


HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.index("\n---\n", 4)
    return cast(dict[str, Any], yaml.safe_load(text[4:end]) or {}), text[end + 5 :]


def slug(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-") or "section"


def _blocks(lines: list[str]) -> list[Block]:
    blocks: list[Block] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            if all(line.startswith("|") for line in current):
                kind: BlockKind = "table"
            elif all(line.startswith("- ") for line in current):
                kind = "list"
            else:
                kind = "text"
            blocks.append(Block(kind, tuple(current)))
            current.clear()

    for line in lines:
        if line.strip():
            current.append(line.rstrip())
        else:
            flush()
    flush()
    return blocks


def parse(text: str, doc_id: str | None = None) -> Parsed:
    """Markdown into sections, one per heading. Anchors are doc_id#slug, unique within the document."""
    meta, body = split_front_matter(text)
    doc_id = doc_id or str(meta["doc_id"])
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    seen: dict[str, int] = {}
    heading: str | None = None
    pending: list[str] = []

    def close() -> None:
        blocks = _blocks(pending)
        pending.clear()
        if heading is None and not blocks:
            return
        name = heading or str(meta.get("title") or doc_id)
        base = slug(name)
        seen[base] = seen.get(base, 0) + 1
        anchor = f"{doc_id}#{base}" if seen[base] == 1 else f"{doc_id}#{base}-{seen[base]}"
        path = tuple(title for _, title in stack) if heading else (name,)
        sections.append(Section(anchor, name, path, len(sections) + 1, tuple(blocks)))

    for line in body.splitlines():
        match = HEADING.match(line)
        if not match:
            pending.append(line)
            continue
        close()
        level, heading = len(match.group(1)), match.group(2)
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading))
    close()
    return Parsed(meta, sections)
