import re

from app.ingest.mask import TOKENS, Span, mask
from app.seed.notes import ClaimFacts, Note, canary


def _covered(note: Note, masked_spans: tuple[Span, ...], kind: str, text: str) -> bool:
    starts = [m.start() for m in re.finditer(re.escape(text), note.body)]
    return bool(starts) and all(
        any(s.start <= i and i + len(text) <= s.end and s.kind == kind for s in masked_spans) for i in starts
    )


def test_every_planted_identifier_is_masked_as_its_own_kind(
    notes: list[Note], claim_facts: dict[int, ClaimFacts]
) -> None:
    planted = missed = 0
    for note in notes:
        masked = mask(note.body, [claim_facts[note.claim_id].holder])
        for kind, text in note.pii:
            planted += 1
            if not _covered(note, masked.spans, kind, text):
                missed += 1
            assert text not in masked.text or kind == "name", (note.doc_id, kind, text)
    assert planted > 150
    assert missed == 0, f"recall {1 - missed / planted:.3f}"


def test_nothing_is_masked_that_was_not_planted(notes: list[Note], claim_facts: dict[int, ClaimFacts]) -> None:
    spans = wrong = 0
    for note in notes:
        masked = mask(note.body, [claim_facts[note.claim_id].holder])
        for span in masked.spans:
            spans += 1
            found = note.body[span.start : span.end]
            if not any(text in found or found in text for _, text in note.pii):
                wrong += 1
    assert wrong == 0, f"precision {1 - wrong / spans:.3f}"


def test_claim_numbers_amounts_loss_dates_and_canaries_survive(
    notes: list[Note], claim_facts: dict[int, ClaimFacts]
) -> None:
    for note in notes:
        claim = claim_facts[note.claim_id]
        masked = mask(note.body, [claim.holder]).text
        keep = re.findall(r"\$[\d,]+(?:\.\d{2})?|DOL \d{2}/\d{2}/\d{4}|\b\d{6}\b", note.body)
        keep.append(canary(note.region))
        for text in keep:
            assert text in masked, (note.doc_id, text)


def test_dates_are_masked_only_as_dates_of_birth() -> None:
    text = "DOL 04/12/2025, reported 04/14/2025. Verified ID, DOB is 03/14/1961. Born 3/14/61."
    assert mask(text).text == "DOL 04/12/2025, reported 04/14/2025. Verified ID, DOB is [DOB]. Born [DOB]."


def test_scan_text_loses_the_policyholder_but_keeps_the_vendor_and_money() -> None:
    ocr_text = "Claim No. 100357\nInsured Dwayne Ochoa\nContractor Mile High Restoration\nNet amount claimed $5,386.35"
    masked = mask(ocr_text, ["Dwayne Ochoa"]).text
    assert masked == (
        "Claim No. 100357\nInsured [NAME]\nContractor Mile High Restoration\nNet amount claimed $5,386.35"
    )


def test_phone_and_ssn_shapes_are_told_apart() -> None:
    text = "Call 303-555-0142 or (303) 555-0143 or 3035550144. SSN 615-00-3007, SSN is 615003007, pol 5009446."
    masked = mask(text)
    assert masked.text == f"Call {TOKENS['phone']} or [PHONE] or [PHONE]. SSN [SSN], SSN is [SSN], pol 5009446."
    assert masked.counts == {"phone": 3, "ssn": 2}
