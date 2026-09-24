from decimal import Decimal
from typing import Any

import pytest

from app.ingest import ocr
from app.seed.scans import SCANS_DIR
from tests.docs.requires import needs_tesseract

HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"


def page(*lines: str, conf: float = 96.0) -> list[ocr.Word]:
    """A tesseract TSV page with one word box per word, laid out left to right."""
    rows = [HEADER]
    for n, line in enumerate(lines, 1):
        x = 100
        for w, word in enumerate(line.split(), 1):
            rows.append(f"5\t1\t1\t1\t{n}\t{w}\t{x}\t{n * 50}\t{len(word) * 20}\t30\t{conf}\t{word}")
            x += len(word) * 20 + 15
    return ocr.parse_tsv("\n".join(rows))


INVOICE = (
    "Front Range Roofing INVOICE",
    "Invoice date 05/02/2025",
    "Claim No. 104161",
    "Description Amount",
    "Tear off and replace laminated shingles $6,480.00",
    "Replace gutters and downspouts $2,930.38",
    "Total due {total}",
    "Remit to: Front Range Roofing, Denver, CO",
)


def fields_for(total: str, ledger: str = "9410.38", conf: float = 96.0) -> dict[str, ocr.Field]:
    words = page(*(line.format(total=total) for line in INVOICE), conf=conf)
    return ocr.check(ocr.extract(words), 104161, Decimal(ledger))


def test_fields_are_read_by_the_label_to_their_left() -> None:
    fields = fields_for("$9,410.38")
    assert {name: f.value for name, f in fields.items()} == {
        "claim_number": "104161",
        "date": "2025-05-02",
        "vendor": "Front Range Roofing",
        "total": "9410.38",
        "line_1": "Tear off and replace laminated shingles | 6480.00",
        "line_2": "Replace gutters and downspouts | 2930.38",
    }
    assert not any(f.flagged for f in fields.values())
    assert fields["total"].bbox is not None and fields["total"].confidence == 96.0


def test_a_total_that_disagrees_with_the_payments_is_flagged() -> None:
    total = fields_for("$9,140.38")["total"]
    assert total.flag_reason == "does not match payments, line items do not add up"


def test_a_total_without_its_decimal_point_fails_the_format_check() -> None:
    total = fields_for("$941038")["total"]
    assert (total.value, total.flag_reason) == ("$941038", "format")


def test_low_confidence_is_flagged_even_when_the_value_parses() -> None:
    assert fields_for("$9,410.38", conf=ocr.MIN_CONFIDENCE - 5)["total"].flags == ["low confidence"]


def test_a_missing_label_and_a_claim_number_for_another_claim_are_flagged() -> None:
    words = page("Claim No. 104162", "Net amount due $10.00")
    fields = ocr.check(ocr.extract(words), 104161, Decimal("10.00"))
    assert fields["claim_number"].flags == ["does not match the claim"]
    assert fields["total"].flags == ["missing"] and fields["vendor"].flags == ["missing"]


def _scan(truth: list[dict[str, Any]], tag: str) -> dict[str, Any]:
    return next(row for row in truth if (row["mismatch"] or row["degraded"] or "clean") == tag)


@needs_tesseract
@pytest.mark.parametrize("tag", ["clean", "transposed", "dropped-decimal", "smudge"])
def test_committed_scans_read_back_with_the_expected_flags(truth: list[dict[str, Any]], tag: str) -> None:
    row = _scan(truth, tag)
    fields = ocr.check(
        ocr.extract(ocr.tesseract(SCANS_DIR / row["file"])), row["claim_id"], Decimal(row["ledger_total"])
    )
    assert fields["claim_number"].value == row["claim_number"]
    assert fields["vendor"].value == row["vendor"]
    assert fields["date"].value == row["date"]
    total = fields["total"]
    if tag == "clean":
        assert (total.value, total.flagged) == (row["total"], False)
    elif tag == "transposed":
        assert total.value == row["total"] and "does not match payments" in total.flags
    else:
        assert "format" in total.flags
