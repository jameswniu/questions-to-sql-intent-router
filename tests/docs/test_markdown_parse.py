from app.ingest.chunk import chunk
from app.ingest.parse import parse
from app.seed.documents import CORPUS_DIR

HO_2025 = (CORPUS_DIR / "wordings" / "ho-2025.md").read_text()


def test_anchors_are_unique_stable_and_prefixed_with_the_document() -> None:
    first, second = parse(HO_2025), parse(HO_2025)
    anchors = [s.anchor for s in first.sections]
    assert anchors == [s.anchor for s in second.sections]
    assert len(set(anchors)) == len(anchors)
    assert all(anchor.startswith("ho-2025#") for anchor in anchors)
    assert "ho-2025#5-2-windstorm-or-hail-deductible" in anchors


def test_repeated_headings_get_numbered_anchors() -> None:
    text = "---\ndoc_id: d\ntitle: D\n---\n\n# D\n\n## Notes\n\nOne.\n\n## Notes\n\nTwo.\n"
    assert [s.anchor for s in parse(text).sections] == ["d#d", "d#notes", "d#notes-2"]


def test_heading_path_runs_from_the_title_to_the_section() -> None:
    section = next(s for s in parse(HO_2025).sections if s.heading.startswith("5.2"))
    assert section.path == ("Homeowners Policy, Form HO-2025", "5. Deductibles", "5.2 Windstorm or hail deductible")


def test_ordinals_count_sections_and_chunks_in_reading_order() -> None:
    parsed = parse(HO_2025)
    assert [s.ordinal for s in parsed.sections] == list(range(1, len(parsed.sections) + 1))
    assert [c.ordinal for c in chunk(parsed.sections)] == list(range(1, len(chunk(parsed.sections)) + 1))


def test_front_matter_carries_edition_dates() -> None:
    meta = parse(HO_2025).meta
    assert (meta["edition"], str(meta["effective_from"]), meta["effective_to"]) == ("HO-2025", "2025-01-01", None)


def test_tables_and_lists_are_kept_as_blocks() -> None:
    kinds = {b.kind for s in parse(HO_2025).sections for b in s.blocks}
    assert kinds == {"text", "list", "table"}
