from collections import Counter
from datetime import date
from decimal import Decimal

import pytest

from app.config import events, policy
from app.ingest.parse import parse
from app.seed.documents import CORPUS_DIR, corpus, stale_files, usd
from app.seed.rows import Dataset


def section(doc: str, heading_prefix: str) -> str:
    text = (CORPUS_DIR / "wordings" / f"{doc}.md").read_text()
    found = next(s for s in parse(text).sections if s.heading.startswith(heading_prefix))
    return "\n".join(block.text for block in found.blocks)


def test_committed_corpus_is_what_the_generator_renders(dataset: Dataset) -> None:
    assert stale_files(corpus(dataset)) == []


FACT_PLACES = {
    "ho2023_wind_hail_deductible": [("ho-2023", "5.2")],
    "ho2025_wind_hail_deductible_west_south": [("ho-2025", "5.2")],
    "ho2025_wind_hail_deductible_elsewhere": [("ho-2025", "5.2")],
    "ho2023_mold_sublimit": [("ho-2023", "9.")],
    "ho2025_mold_sublimit": [("ho-2025", "9.")],
    "ho2025_roof_acv_age": [("ho-2025", "6.2")],
    "flood_excluded": [("ho-2023", "4.1"), ("ho-2025", "4.1")],
    "notice_window": [("ho-2023", "7."), ("ho-2025", "7.")],
}


@pytest.mark.parametrize("fact", sorted(policy()["facts"]))
def test_every_golden_fact_is_on_the_page_of_its_edition(fact: str) -> None:
    for doc, heading in FACT_PLACES[fact]:
        assert policy()["facts"][fact] in section(doc, heading), (fact, doc, heading)


def test_the_two_editions_differ_where_policy_yaml_says_they_do() -> None:
    old, new = policy()["editions"]["HO-2023"], policy()["editions"]["HO-2025"]
    assert "2% of Coverage A" not in section("ho-2023", "5.2")
    assert usd(old["mold_sublimit"]) not in section("ho-2025", "9.")
    assert "actual cash value" in section("ho-2025", "6.2") and "actual cash value" not in section("ho-2023", "6.2")
    assert old["effective_to"] < new["effective_from"]


def test_each_event_has_one_document_with_its_ref_and_title() -> None:
    for event in events():
        doc = event["doc"]
        text = (CORPUS_DIR / f"{doc['kind']}s" / f"{doc['ref'].lower()}.md").read_text()
        meta = parse(text).meta
        assert (meta["ref"], meta["title"], meta["kind"]) == (doc["ref"], doc["title"], doc["kind"])
        if doc["kind"] == "memo":
            assert all(f"\n{label}: " in text for label in ("To", "From", "Date", "Re")), doc["ref"]
        else:
            assert "\nIssued " in text, doc["ref"]


def test_event_documents_quote_the_numbers_in_the_seeded_rows(dataset: Dataset) -> None:
    def read(ref: str, kind: str) -> str:
        return (CORPUS_DIR / f"{kind}s" / f"{ref.lower()}.md").read_text()

    freeze = [
        c
        for c in dataset.claims
        if c[2] == "North" and c[4] == "water" and date(2025, 1, 20) <= c[5] <= date(2025, 1, 24)
    ]
    hail = [
        c for c in dataset.claims if c[3] == "CO" and c[4] == "hail" and date(2025, 4, 12) <= c[5] <= date(2025, 4, 13)
    ]
    voided = [p for p in dataset.payments if p[6] == "voided"]
    assert f"we have {len(freeze)} water claims" in read("CAT-25-02", "bulletin")
    assert f"we have {len(hail)} hail claims in Colorado" in read("CAT-25-07", "bulletin")
    outage = read("OPS-26-03", "memo")
    assert f"The {len(voided)} payments" in outage
    assert usd(sum((p[4] for p in voided), Decimal(0)), cents=True) in outage
    assert "May 26 to June 5, 2026" in outage
    assert "about 22 percent" in read("CL-25-14", "memo")
    assert Counter(c[3] for c in freeze).keys() == {"MN", "WI"}


def test_static_documents_are_general_and_carry_no_claim() -> None:
    for path in CORPUS_DIR.rglob("*.md"):
        meta = parse(path.read_text()).meta
        assert "claim_id" not in meta and "region" not in meta, path.name
