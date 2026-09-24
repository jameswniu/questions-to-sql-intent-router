from collections.abc import Sequence

from app.answer.passages import Passage, choose, matches, pieces, score, split, terms
from app.sources.documents import Hit


def hit(chunk_id: str, body: str, header: str = "", kind: str = "wording") -> Hit:
    doc_id = chunk_id.partition("#")[0]
    return Hit(chunk_id, doc_id, chunk_id.rpartition(":")[0], doc_id, header, body, kind, 0.0, None, None, False, None)


FLOOD = hit(
    "ho#4-1:1",
    "Flood, surface water, waves or overflow of a body of water. Water below the ground that seeps through a "
    "floor is also excluded. This exclusion does not apply to water that escapes from a plumbing system.",
    header="Form | 4. Exclusions > 4.1 Flood and surface water",
)
RESERVING = hit(
    "cg#1:1",
    "Set the reserve at the covered damage less the deductible. Review it when a new estimate arrives.",
    header="Guidelines | 1. Reserving",
    kind="guideline",
)
DWELLING = hit(
    "ho#2-1:1",
    "We cover sudden damage to the dwelling. Covered damage is paid at replacement cost.",
    header="Form | 2.1 Coverage A, Dwelling",
)
TABLE = hit(
    "ho#5-3:1",
    "| State | Peril | Deductible |\n| --- | --- | --- |\n| Texas | Hail | 2% |\n| Texas | Fire | $1,000 |",
    header="Form | 5.3 Deductible schedule",
)


def texts(chosen: Sequence[Passage]) -> list[str]:
    return [p.text for p in chosen]


def test_a_table_row_keeps_its_column_names() -> None:
    assert pieces(TABLE.body) == [
        ("State: Texas; Peril: Hail; Deductible: 2%.", True),
        ("State: Texas; Peril: Fire; Deductible: $1,000.", True),
    ]


def test_a_list_item_stays_whole_and_a_memo_address_block_is_dropped() -> None:
    body = "To: All staff\nDate: June 8, 2026\n\n- Give notice within 60 days. A late loss may be denied.\n"
    body += "- Call the police."
    assert split(body) == ["Give notice within 60 days. A late loss may be denied.", "Call the police."]


def test_prose_splits_into_sentences() -> None:
    assert split("The first rule applies. 1.2 The second one too.") == [
        "The first rule applies.",
        "1.2 The second one too.",
    ]


def test_word_forms_meet_but_lookalikes_do_not() -> None:
    assert matches("roof", terms("roofs and roofing"))
    assert matches("report", terms("a loss reported late"))
    assert matches("escalate", terms("record the escalation"))
    assert not matches("flood", terms("seeps through a floor"))
    assert terms("How much is the HO-2025 deductible?") == frozenset({"2025", "deductible"})


def test_a_rare_question_word_outweighs_two_common_ones() -> None:
    chosen = choose(score([RESERVING, DWELLING, FLOOD], "Is flood damage covered?"))
    assert chosen[0].hit is FLOOD
    assert texts(chosen)[0].startswith("Flood, surface water")


def test_a_lone_short_passage_brings_its_neighbour_for_context() -> None:
    short = hit("cg#3:1", "Hail is covered. Roofs are inspected within ten days. Gutters are checked last.")
    chosen = choose(score([short], "Is hail covered?"))
    assert texts(chosen) == ["Hail is covered.", "Roofs are inspected within ten days."]


def test_a_question_whose_words_are_mostly_absent_goes_unanswered() -> None:
    assert choose(score([FLOOD], "Is flood damage covered?")) == []


def test_the_matching_table_row_answers_alone() -> None:
    chosen = choose(score([TABLE], "What is the hail deductible in Texas?"))
    assert texts(chosen) == ["State: Texas; Peril: Hail; Deductible: 2%."]


def test_nothing_is_chosen_when_the_question_shares_no_word() -> None:
    assert choose(score([FLOOD, RESERVING], "Does the policy cover cryptocurrency wallets?")) == []


def test_a_confident_cross_encoder_outranks_shared_words() -> None:
    def rerank(query: str, texts: Sequence[str]) -> list[float]:
        return [8.0 if "plumbing" in text else -8.0 for text in texts]

    chosen = choose(score([FLOOD], "What about water?", rerank=rerank))
    assert "plumbing" in max(chosen, key=lambda p: p.score).text


def test_every_cross_encoder_rejection_means_not_found() -> None:
    def rerank(query: str, texts: Sequence[str]) -> list[float]:
        return [-10.0] * len(texts)

    assert choose(score([FLOOD], "Is flood damage covered?", rerank=rerank)) == []


def test_chosen_passages_read_in_document_order() -> None:
    chosen = choose(score([FLOOD], "flood water excluded plumbing"), most=3)
    positions = [p.position for p in chosen]
    assert positions == sorted(positions)
