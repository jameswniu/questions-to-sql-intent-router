import hashlib
from collections import Counter
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.config import events, policy
from app.seed import rows
from app.seed.rows import Dataset, generate


def digest(data: Dataset) -> str:
    return hashlib.sha256(repr(list(data.tables())).encode()).hexdigest()


def event(event_id: str) -> dict[str, Any]:
    return next(e for e in events() if e["id"] == event_id)


@pytest.fixture(scope="module")
def data() -> Dataset:
    return generate()


def test_same_seed_generates_the_same_rows(data: Dataset) -> None:
    assert digest(generate()) == digest(data)
    assert digest(generate(seed=1)) != digest(data)


def test_no_generated_ssn_could_belong_to_a_real_person(data: Dataset) -> None:
    # SSA never issues area 000, 666 or 900-999, group 00, or serial 0000.
    for row in data.policyholders:
        area, group, serial = row[5].split("-")
        assert area in ("000", "666") or area >= "900" or group == "00" or serial == "0000", row[5]


def test_claim_volume_stays_in_the_designed_range(data: Dataset) -> None:
    planted = sum(e.get("extra_claims", 0) for e in events())
    assert 5000 <= len(data.claims) - planted <= 7000


def test_front_range_hail_is_visible_in_colorado_claims(data: Dataset) -> None:
    hail = event("hail-front-range-2025")
    first, last = hail["loss_dates"]
    in_storm = [c for c in data.claims if c[3] == "CO" and c[4] == "hail" and first <= c[5] <= last]
    assert len(in_storm) >= hail["extra_claims"]


def test_freeze_adds_north_water_claims_in_its_week(data: Dataset) -> None:
    freeze = event("pipe-freeze-2025-01")
    first, last = freeze["loss_dates"]
    in_week = [c for c in data.claims if c[2] == "North" and c[4] == "water" and first <= c[5] <= last]
    assert len(in_week) >= freeze["extra_claims"]


def test_outage_voids_every_payment_in_its_window_and_reissues_it(data: Dataset) -> None:
    outage = event("payments-outage-2026-05")
    start, end = outage["window"]
    reissue_start, reissue_end = outage["reissue_between"]
    in_window = [p for p in data.payments if start <= p[3] <= end]
    assert in_window
    assert all(p[6] == "voided" for p in in_window)
    reissued = [p for p in data.payments if p[6] == "reissue"]
    assert all(reissue_start <= p[3] <= reissue_end for p in reissued)
    assert Counter((p[1], p[4], p[5]) for p in in_window) == Counter((p[1], p[4], p[5]) for p in reissued)


# Before the outage, inside it, between it and the reissues, inside the reissue window, and the default.
AS_OF_AROUND_THE_OUTAGE = [date(2026, 5, 1), date(2026, 5, 15), date(2026, 5, 24), date(2026, 5, 30), date(2026, 7, 15)]


@pytest.mark.parametrize("as_of", AS_OF_AROUND_THE_OUTAGE, ids=str)
def test_no_row_is_dated_after_as_of(as_of: date) -> None:
    late = [
        (table, row)
        for table, _, table_rows in generate(as_of=as_of).tables()
        for row in table_rows
        if any(isinstance(value, date) and value > as_of for value in row)
    ]
    assert late == []


@pytest.mark.parametrize("as_of", AS_OF_AROUND_THE_OUTAGE, ids=str)
def test_voided_payments_are_reissued_only_once_the_reissue_has_happened(as_of: date) -> None:
    payments = generate(as_of=as_of).payments
    voided = Counter((p[1], p[4], p[5]) for p in payments if p[6] == "voided")
    reissued = Counter((p[1], p[4], p[5]) for p in payments if p[6] == "reissue")
    assert reissued <= voided
    first_reissue, last_reissue = event("payments-outage-2026-05")["reissue_between"]
    if as_of < first_reissue:
        assert not reissued
    if as_of >= last_reissue:
        assert reissued == voided


def test_default_as_of_keeps_the_published_row_counts(data: Dataset) -> None:
    # Pinned so that a change to the default dataset, and to every number computed from it, is deliberate.
    assert {table: len(table_rows) for table, _, table_rows in data.tables()} == {
        "regions": 4,
        "states": 8,
        "policyholders": 12000,
        "policies": 12000,
        "adjusters": 16,
        "claims": 6951,
        "payments": 10656,
        "earned_premium": 120,
    }


def test_payment_region_is_its_claims_region(data: Dataset) -> None:
    region_of = {c[0]: c[2] for c in data.claims}
    assert all(p[2] == region_of[p[1]] for p in data.payments)


def test_edition_and_deductible_follow_the_loss_date(data: Dataset) -> None:
    coverage = {p[0]: p[5] for p in data.policies}
    new_edition = policy()["editions"]["HO-2025"]["effective_from"]
    for c in data.claims:
        claim_id, policy_id, region, peril, loss_date, edition, ded = c[0], c[1], c[2], c[4], c[5], c[10], c[11]
        assert edition == ("HO-2025" if loss_date >= new_edition else "HO-2023"), claim_id
        if edition == "HO-2025" and peril in ("wind", "hail") and region in ("West", "South"):
            assert ded == (coverage[policy_id] * Decimal("0.02")).quantize(Decimal("0.01")), claim_id
        else:
            assert ded == Decimal("1000.00"), claim_id


def test_claims_reported_long_before_as_of_are_mostly_settled(data: Dataset) -> None:
    as_of = policy()["as_of"]
    old = [c for c in data.claims if (as_of - c[6]).days > 90]
    assert sum(c[8] == "open" for c in old) / len(old) < 0.05


def test_open_claims_carry_no_closing_date_and_denials_carry_a_reason(data: Dataset) -> None:
    for c in data.claims:
        status, closed, reason = c[8], c[7], c[14]
        assert (status == "open") == (closed is None), c[0]
        if status == "denied":
            assert reason in ("below_deductible", "excluded_peril", "late_notice"), c[0]


def test_roofing_surge_multiplies_only_colorado_roof_claims_closed_in_its_quarter(
    data: Dataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    shift = event("roofing-surge-2025q3")
    flat = [dict(e, severity_multiplier=1.0) if e["id"] == shift["id"] else e for e in events()]
    monkeypatch.setattr(rows, "events", lambda: flat)
    unshifted = generate()
    first, last = shift["closed_between"]
    surged = 0
    for claim, before in zip(data.claims, unshifted.claims, strict=True):
        state, peril, status, closed = claim[3], claim[4], claim[8], claim[7]
        if state == shift["state"] and peril in shift["perils"] and status == "closed" and first <= closed <= last:
            surged += 1
            assert claim[13] == (before[13] * Decimal("1.22")).quantize(Decimal("0.01")), claim[0]
        else:
            assert claim == before
    assert surged > 0
