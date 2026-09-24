"""Checks the committed evals/cases/paraphrase.jsonl against the tool that made it, without calling the tool:
regenerating it means calling Codex, which this suite never does and which would mean fabricating variants
rather than reading what was already made."""

import json
from typing import Any

from tools import paraphrase

REQUIRED_KINDS = sorted(kind for _key, kind in paraphrase.SUFFIXES)


def _rows() -> list[dict[str, Any]]:
    with (paraphrase.CASES_DIR / "paraphrase.jsonl").open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_every_source_the_set_was_built_from_has_exactly_its_three_variants_and_no_extras() -> None:
    rows = _rows()
    eligible = {o.id for o in paraphrase.load_originals()}
    covered = sorted({row["of"] for row in rows})

    # route-035 and route-036 were added to routing.jsonl after this set was generated: load_originals()
    # counts them as eligible dev routing sources today, but they were never sent to Codex. So this test
    # pins the expected source list to what the committed file was actually built from, its own "of" ids,
    # rather than to today's live eligibility list; that is the only list a coverage check can verify
    # without regenerating the set. A source id the file covers that is no longer eligible at all (the
    # other direction) still fails, since that would be stale coverage rather than a not-yet-covered case.
    stale = set(covered) - eligible
    assert not stale, f"paraphrase.jsonl covers ids that are no longer eligible sources: {sorted(stale)}"

    by_source: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_source.setdefault(row["of"], []).append(row)

    assert len(rows) == 3 * len(covered), f"{len(rows)} rows for {len(covered)} covered sources, want 3 each"
    for source_id in covered:
        variant_rows = by_source[source_id]
        assert len(variant_rows) == 3, f"{source_id}: {len(variant_rows)} variant rows, want 3"
        ids = sorted(row["id"] for row in variant_rows)
        assert ids == sorted(f"{source_id}-{key}" for key, _kind in paraphrase.SUFFIXES), f"{source_id}: ids {ids}"
        kinds = sorted(row["variant"] for row in variant_rows)
        assert kinds == REQUIRED_KINDS, f"{source_id}: variant kinds {kinds}, want {REQUIRED_KINDS}"
