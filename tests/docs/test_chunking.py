from pathlib import Path

import pytest

from app.ingest.chunk import MAX_TOKENS, approx_tokens, chunk, sentences
from app.ingest.parse import Block, Section, parse
from app.seed.documents import CORPUS_DIR

CORPUS = sorted(CORPUS_DIR.rglob("*.md"))


@pytest.mark.parametrize("path", CORPUS, ids=lambda p: p.name)
def test_no_chunk_exceeds_the_token_budget(path: Path) -> None:
    chunks = chunk(parse(path.read_text()).sections)
    assert chunks
    assert max(approx_tokens(c.body) for c in chunks) <= MAX_TOKENS


@pytest.mark.parametrize("path", CORPUS, ids=lambda p: p.name)
def test_every_sentence_lands_whole_in_one_chunk(path: Path) -> None:
    sections = parse(path.read_text()).sections
    bodies = [c.body for c in chunk(sections)]
    for section in sections:
        for block in section.blocks:
            if block.kind == "table":
                continue
            for line in block.lines:
                for sentence in sentences(line):
                    assert any(sentence in body for body in bodies), sentence


def test_oversized_table_splits_into_row_groups_that_each_repeat_the_header() -> None:
    sections = parse((CORPUS_DIR / "wordings" / "ho-2025.md").read_text()).sections
    table = next(b for s in sections for b in s.blocks if b.kind == "table")
    assert approx_tokens(table.text) > MAX_TOKENS
    groups = [c.body for c in chunk(sections) if c.body.startswith("| Region |")]
    assert len(groups) >= 2
    rows: list[str] = []
    for group in groups:
        lines = group.splitlines()
        assert lines[:2] == list(table.lines[:2])
        rows += lines[2:]
    assert rows == list(table.lines[2:])


def test_a_paragraph_over_budget_breaks_between_sentences() -> None:
    sentence = "The adjuster confirmed the loss and recorded the moisture readings in every affected room."
    long = " ".join([sentence] * 40)
    section = Section("d#s", "S", ("S",), 1, (Block("text", (long,)),))
    chunks = chunk([section], limit=100)
    assert len(chunks) > 1
    assert all(c.body.endswith("room.") and approx_tokens(c.body) <= 100 for c in chunks)
    assert " ".join(c.body for c in chunks) == long


def test_sentence_splitter_keeps_abbreviations_whole() -> None:
    assert sentences("Paid Valley Restoration Co. in full. Verified ID, D.O.B. 03/14/1961. Closed.") == [
        "Paid Valley Restoration Co. in full.",
        "Verified ID, D.O.B. 03/14/1961.",
        "Closed.",
    ]
