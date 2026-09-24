import math
import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from faker import Faker
from psycopg import sql

from app.config import events, policy, settings

SEED = 20260715
HOLDERS_PER_STATE = 1500
ADJUSTERS_PER_REGION = 4
CHANNELS = (("phone", 0.35), ("web", 0.30), ("agent", 0.25), ("app", 0.10))

# Baseline claims per state per month before seasonality, in policy.yaml peril order:
# water, wind, hail, fire, theft, mold.
BASE_RATES = {
    "MN": (10.5, 3.8, 2.2, 1.8, 3.8, 1.2),
    "WI": (9.8, 3.8, 2.2, 1.8, 3.0, 1.2),
    "TX": (6.0, 7.5, 9.0, 1.8, 6.0, 1.8),
    "OK": (5.2, 7.5, 9.0, 1.8, 3.8, 1.2),
    "PA": (9.0, 4.5, 1.2, 1.8, 5.2, 1.2),
    "OH": (8.2, 4.5, 1.5, 1.8, 5.2, 1.2),
    "CO": (5.2, 5.2, 12.0, 1.8, 4.5, 1.2),
    "AZ": (4.5, 3.8, 2.2, 1.8, 5.2, 1.2),
}

# Monthly multipliers, January first. Frozen pipes drive winter water losses in the North,
# and the hail belt (CO, TX, OK) takes most of its hail from April to July.
SEASON = {
    "water_north": (2.0, 1.8, 1.3, 0.9, 0.7, 0.6, 0.6, 0.6, 0.7, 0.9, 1.2, 1.7),
    "water": (1.3, 1.2, 1.0, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 1.0, 1.0, 1.2),
    "wind": (0.8, 1.0, 1.5, 1.7, 1.4, 1.1, 0.9, 0.8, 0.8, 0.9, 0.9, 0.8),
    "hail_belt": (0.1, 0.2, 0.6, 1.8, 2.6, 2.4, 1.9, 1.0, 0.5, 0.3, 0.1, 0.1),
    "hail": (0.2, 0.2, 0.5, 1.2, 1.8, 2.0, 2.0, 1.6, 0.9, 0.5, 0.3, 0.2),
    "fire": (1.3, 1.2, 1.0, 0.9, 0.8, 0.8, 0.9, 0.9, 0.9, 1.0, 1.1, 1.3),
    "theft": (0.8, 0.8, 0.9, 1.0, 1.1, 1.2, 1.2, 1.2, 1.0, 1.0, 0.9, 0.9),
    "mold": (0.8, 0.8, 0.9, 1.0, 1.1, 1.2, 1.2, 1.2, 1.1, 1.0, 0.9, 0.8),
}
HAIL_BELT = {"CO", "TX", "OK"}

# Median damage and lognormal sigma by peril.
DAMAGE = {
    "water": (9000, 0.8),
    "wind": (7000, 0.7),
    "hail": (11000, 0.6),
    "fire": (30000, 1.0),
    "theft": (3500, 0.7),
    "mold": (6000, 0.6),
}
COVERAGE_A_MEDIAN = {
    "MN": 280_000, "WI": 250_000, "TX": 260_000, "OK": 190_000,
    "PA": 240_000, "OH": 200_000, "CO": 420_000, "AZ": 330_000,
}  # fmt: skip
# Annual premium per dollar of Coverage A. Claim frequency here runs well above a real book, so
# rates are set high enough to land each region's loss ratio near 65% over the window.
PREMIUM_RATE = {
    "MN": 0.0158, "WI": 0.0145, "TX": 0.0152, "OK": 0.0160,
    "PA": 0.0128, "OH": 0.0135, "CO": 0.0124, "AZ": 0.0070,
}  # fmt: skip
RATE_CHANGE_BY_YEAR = {2024: Decimal("1.00"), 2025: Decimal("1.06"), 2026: Decimal("1.10")}
AREA_CODES = {"MN": "612", "WI": "414", "TX": "512", "OK": "405", "PA": "215", "OH": "614", "CO": "303", "AZ": "602"}

COLUMNS = {
    "regions": ("region",),
    "states": ("state", "name", "region"),
    "policyholders": ("policyholder_id", "first_name", "last_name", "email", "phone", "ssn", "dob", "state"),
    "policies": (
        "policy_id", "policyholder_id", "product", "region", "state", "coverage_a", "original_effective",
        "written_premium",
    ),
    "adjusters": ("adjuster_id", "name", "region"),
    "claims": (
        "claim_id", "policy_id", "region", "state", "peril", "loss_date", "reported_date", "closed_date", "status",
        "adjuster_id", "edition", "deductible", "reserve", "damage_estimate", "denial_reason", "channel",
    ),
    "payments": ("payment_id", "claim_id", "region", "paid_date", "amount", "kind", "status"),
    "earned_premium": ("region", "month", "amount", "exposure"),
}  # fmt: skip


@dataclass
class Dataset:
    """Rows per core table, as tuples in the column order of COLUMNS, listed in foreign-key order."""

    regions: list[tuple[Any, ...]] = field(default_factory=list)
    states: list[tuple[Any, ...]] = field(default_factory=list)
    policyholders: list[tuple[Any, ...]] = field(default_factory=list)
    policies: list[tuple[Any, ...]] = field(default_factory=list)
    adjusters: list[tuple[Any, ...]] = field(default_factory=list)
    claims: list[tuple[Any, ...]] = field(default_factory=list)
    payments: list[tuple[Any, ...]] = field(default_factory=list)
    earned_premium: list[tuple[Any, ...]] = field(default_factory=list)

    def tables(self) -> Iterator[tuple[str, tuple[str, ...], list[tuple[Any, ...]]]]:
        for name, columns in COLUMNS.items():
            yield name, columns, getattr(self, name)


def money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def poisson(rng: random.Random, lam: float) -> int:
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    threshold, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= threshold:
            return k
        k += 1


def pick(rng: random.Random, weighted: tuple[tuple[str, float], ...]) -> str:
    return rng.choices([name for name, _ in weighted], weights=[w for _, w in weighted])[0]


def day_between(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def month_starts(start: date, end: date) -> Iterator[date]:
    month = start.replace(day=1)
    while month <= end:
        yield month
        month = (month + timedelta(days=32)).replace(day=1)


def month_end(month: date) -> date:
    return (month + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def season(peril: str, state: str, region: str) -> tuple[float, ...]:
    if peril == "water":
        return SEASON["water_north" if region == "North" else "water"]
    if peril == "hail":
        return SEASON["hail_belt" if state in HAIL_BELT else "hail"]
    return SEASON[peril]


def edition_in_force(editions: dict[str, Any], loss_date: date) -> str:
    for name, terms in editions.items():
        if terms["effective_from"] <= loss_date <= (terms["effective_to"] or date.max):
            return str(name)
    raise ValueError(f"no edition in force on {loss_date}")


def deductible(terms: dict[str, Any], peril: str, region: str, coverage_a: Decimal) -> Decimal:
    if peril not in ("wind", "hail"):
        return money(terms["all_peril_deductible"])
    rule = terms["wind_hail_deductible"]
    if rule["type"] == "percent_of_coverage_a":
        if region in rule["regions"]:
            return money(coverage_a * Decimal(rule["percent"]) / 100)
        return money(rule["elsewhere_flat"])
    return money(rule["amount"])


def generate(seed: int = SEED, as_of: date | None = None) -> Dataset:
    as_of = as_of or settings().as_of
    rng = random.Random(seed)
    fake = Faker("en_US")
    fake.seed_instance(seed)
    ref = policy()
    start, end = ref["coverage"]["start"], ref["coverage"]["end"]
    regions: dict[str, dict[str, str]] = ref["regions"]
    region_of = {state: region for region, states in regions.items() for state in states}
    by_kind = {event["kind"]: event for event in events() if event["kind"] != "catastrophe"}
    catastrophes = [event for event in events() if event["kind"] == "catastrophe"]

    data = Dataset()
    data.regions = [(region,) for region in regions]
    data.states = [(state, name, region) for region, states in regions.items() for state, name in states.items()]

    policies_by_state: dict[str, list[tuple[int, Decimal]]] = {state: [] for state in region_of}
    for state, region in region_of.items():
        for _ in range(HOLDERS_PER_STATE):
            holder_id = len(data.policyholders) + 1
            first, last = fake.first_name(), fake.last_name()
            data.policyholders.append((
                holder_id,
                first,
                last,
                f"{first}.{last}{holder_id}@example.com".lower(),
                f"{AREA_CODES[state]}-555-01{rng.randint(0, 99):02d}",
                fake.invalid_ssn(),
                day_between(rng, date(1940, 1, 1), date(2000, 12, 31)),
                state,
            ))  # fmt: skip
            coverage = money(round(COVERAGE_A_MEDIAN[state] * math.exp(rng.gauss(0, 0.35)), -3))
            policy_id = 5_000_000 + holder_id
            data.policies.append((
                policy_id,
                holder_id,
                "HO-3",
                region,
                state,
                coverage,
                day_between(rng, date(2005, 1, 1), date(2023, 12, 31)),
                money(coverage * Decimal(str(PREMIUM_RATE[state]))),
            ))  # fmt: skip
            policies_by_state[state].append((policy_id, coverage))

    adjusters_by_region: dict[str, list[int]] = {region: [] for region in regions}
    for region in regions:
        for _ in range(ADJUSTERS_PER_REGION):
            adjuster_id = len(data.adjusters) + 1
            data.adjusters.append((adjuster_id, f"{fake.first_name()} {fake.last_name()}", region))
            adjusters_by_region[region].append(adjuster_id)

    stubs: list[tuple[date, str, str, str]] = []
    for month in month_starts(start, end):
        last_day = min(month_end(month), end)
        for state, region in region_of.items():
            for peril, base in zip(ref["perils"], BASE_RATES[state], strict=True):
                for _ in range(poisson(rng, base * season(peril, state, region)[month.month - 1])):
                    stubs.append((day_between(rng, month, last_day), state, peril, "baseline"))
    for event in catastrophes:
        first_day, last_day = event["loss_dates"]
        states = [event["state"]] if "state" in event else list(regions[event["region"]])
        for _ in range(event["extra_claims"]):
            stubs.append((day_between(rng, first_day, last_day), rng.choice(states), event["peril"], event["id"]))
    stubs.sort(key=lambda stub: stub[:3])

    payments: list[list[Any]] = []
    for loss_date, state, peril, source in stubs:
        reported, late = _report(rng, source, loss_date)
        if reported > as_of:
            continue
        region = region_of[state]
        claim_id = 100_001 + len(data.claims)
        policy_id, coverage = rng.choice(policies_by_state[state])
        edition = edition_in_force(ref["editions"], loss_date)
        terms = ref["editions"][edition]
        ded = deductible(terms, peril, region, coverage)
        outcome = _settle(rng, terms, by_kind["cost_shift"], state, peril, source, reported, late, ded, as_of)
        data.claims.append((
            claim_id,
            policy_id,
            region,
            state,
            peril,
            loss_date,
            reported,
            outcome.closed,
            outcome.status,
            rng.choice(adjusters_by_region[region]),
            edition,
            ded,
            outcome.reserve,
            outcome.damage,
            outcome.reason,
            pick(rng, CHANNELS),
        ))  # fmt: skip
        for paid_date, amount, kind in outcome.payments:
            payments.append([len(payments) + 1, claim_id, region, paid_date, amount, kind, "issued"])

    outage = by_kind["operations"]
    window_start, window_end = outage["window"]
    reissue_start, reissue_end = outage["reissue_between"]
    for row in list(payments):
        if window_start <= row[3] <= window_end:
            row[6] = "voided"
            # Drawn before the as_of check so an earlier as_of leaves the rest of the draws in place.
            reissued = day_between(rng, reissue_start, reissue_end)
            if reissued <= as_of:
                payments.append([len(payments) + 1, row[1], row[2], reissued, row[4], row[5], "reissue"])
    data.payments = [tuple(row) for row in payments]

    written = {region: Decimal(0) for region in regions}
    in_force = dict.fromkeys(regions, 0)
    for _, _, _, region, _, _, _, premium in data.policies:
        written[region] += premium
        in_force[region] += 1
    for month in month_starts(start, end):
        # A month's premium is earned once the month is over, so a month still running at as_of has no row.
        if month_end(month) > as_of:
            break
        for region in regions:
            earned = money(written[region] / 12 * RATE_CHANGE_BY_YEAR[month.year])
            data.earned_premium.append((region, month, earned, in_force[region]))
    return data


@dataclass(frozen=True)
class CatastropheClaims:
    report_days: int
    damage: tuple[float, float]
    paid_between: tuple[date, date] | None


# events.yaml says what happened; this says how the claims it produced reported and settled.
CATASTROPHE_CLAIMS = {
    "pipe-freeze-2025-01": CatastropheClaims(report_days=14, damage=(14000, 0.7), paid_between=None),
    "hail-front-range-2025": CatastropheClaims(
        report_days=21, damage=(18000, 0.5), paid_between=(date(2025, 5, 1), date(2025, 7, 31))
    ),
}


@dataclass(frozen=True)
class Outcome:
    status: str
    closed: date | None
    reserve: Decimal
    damage: Decimal
    reason: str | None
    payments: list[tuple[date, Decimal, str]]


def _report(rng: random.Random, source: str, loss_date: date) -> tuple[date, bool]:
    if source in CATASTROPHE_CLAIMS:
        return loss_date + timedelta(days=rng.randint(1, CATASTROPHE_CLAIMS[source].report_days)), False
    if rng.random() < 0.012:
        return loss_date + timedelta(days=rng.randint(61, 120)), True
    return loss_date + timedelta(days=min(45, round(rng.expovariate(1 / 5)))), False


def _closing_date(rng: random.Random, source: str, peril: str, reported: date) -> date:
    cat = CATASTROPHE_CLAIMS.get(source)
    if cat and cat.paid_between and rng.random() < 0.9:
        first, last = cat.paid_between
        return day_between(rng, max(reported + timedelta(days=10), first), last)
    median = 75 if peril == "fire" else 30
    return reported + timedelta(days=max(3, round(rng.lognormvariate(math.log(median), 0.6))))


def _settle(
    rng: random.Random,
    terms: dict[str, Any],
    cost_shift: dict[str, Any],
    state: str,
    peril: str,
    source: str,
    reported: date,
    late: bool,
    ded: Decimal,
    as_of: date,
) -> Outcome:
    median, sigma = CATASTROPHE_CLAIMS[source].damage if source in CATASTROPHE_CLAIMS else DAMAGE[peril]
    damage = money(median * math.exp(rng.gauss(0, sigma)))
    reason = None
    if late:
        reason = "late_notice"
    elif peril == "water" and source == "baseline" and rng.random() < 0.05:
        reason = "excluded_peril"
    elif damage <= ded:
        reason = "below_deductible"
    if reason is not None:
        decided = reported + timedelta(days=rng.randint(5, 30))
        if decided > as_of:
            return Outcome("open", None, money(0), damage, None, [])
        return Outcome("denied", decided, money(0), damage, reason, [])

    closed = _closing_date(rng, source, peril, reported)
    shift_start, shift_end = cost_shift["closed_between"]
    if state == cost_shift["state"] and peril in cost_shift["perils"] and shift_start <= closed <= shift_end:
        damage = money(damage * Decimal(str(cost_shift["severity_multiplier"])))
    payable = damage - ded
    capped = peril == "mold" and payable > terms["mold_sublimit"]
    if capped:
        payable = money(terms["mold_sublimit"])

    if closed > as_of:
        advances = []
        if (as_of - reported).days > 30 and rng.random() < 0.4:
            advance = money(payable * Decimal(str(rng.uniform(0.3, 0.5))))
            advances.append((day_between(rng, reported + timedelta(days=14), as_of), advance, "indemnity"))
            payable -= advance
        return Outcome("open", None, payable, damage, None, advances)

    # Drawn whether or not they are used, so an amount never changes which draws come next.
    split, split_share, split_at = rng.random() < 0.4, rng.uniform(0.3, 0.6), rng.uniform(0.3, 0.7)
    paid = []
    if split and payable >= 5000:
        first = money(payable * Decimal(str(split_share)))
        first_date = reported + timedelta(days=round((closed - reported).days * split_at))
        paid += [(first_date, first, "indemnity"), (closed, payable - first, "indemnity")]
    else:
        paid.append((closed, payable, "indemnity"))
    if rng.random() < 0.45:
        paid.append((day_between(rng, reported, closed), money(rng.uniform(150, 900)), "expense"))
    return Outcome("closed", closed, money(0), damage, "mold_sublimit" if capped else None, paid)


def load(conn: psycopg.Connection[Any], data: Dataset) -> dict[str, int]:
    counts = {}
    with conn.cursor() as cur:
        for table, columns, rows in data.tables():
            statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier("core", table), sql.SQL(", ").join(map(sql.Identifier, columns))
            )
            with cur.copy(statement) as copy:
                for row in rows:
                    copy.write_row(row)
            counts[table] = len(rows)
    return counts
