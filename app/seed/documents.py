import argparse
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

import yaml

from app.config import DATA_DIR, events, policy
from app.seed import rows
from app.seed.rows import Dataset

CORPUS_DIR = DATA_DIR / "corpus"

# Illustrative Coverage A limits for the deductible schedule. Every other number on a wording
# page comes from data/policy.yaml.
SCHEDULE_LIMITS = (Decimal(250_000), Decimal(400_000))
PERIL_NAMES = {
    "water": "Water damage",
    "wind": "Windstorm",
    "hail": "Hail",
    "fire": "Fire or lightning",
    "theft": "Theft",
    "mold": "Fungi, mold or wet rot",
}


@dataclass(frozen=True)
class Document:
    path: str
    meta: dict[str, Any]
    body: str

    def markdown(self) -> str:
        front = yaml.safe_dump(self.meta, sort_keys=False, default_flow_style=False).strip()
        return f"---\n{front}\n---\n\n{self.body.strip()}\n"


def usd(amount: Decimal | int | float, cents: bool = False) -> str:
    value = Decimal(str(amount))
    return f"${value:,.2f}" if cents or value != value.to_integral_value() else f"${value:,.0f}"


def long_date(day: date) -> str:
    return f"{day:%B} {day.day}, {day.year}"


def span(first: date, last: date, joiner: str = "to") -> str:
    if (first.year, first.month) == (last.year, last.month):
        return f"{first:%B} {first.day} {joiner} {last.day}, {last.year}"
    if first.year == last.year:
        return f"{first:%B} {first.day} {joiner} {last:%B} {last.day}, {last.year}"
    return f"{long_date(first)} {joiner} {long_date(last)}"


def spoken_list(items: Iterable[str]) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _states(region: str) -> list[str]:
    return list(policy()["regions"][region].values())


def _all_states() -> list[str]:
    return [name for states in policy()["regions"].values() for name in states.values()]


def roof_acv_age(terms: dict[str, Any]) -> int | None:
    found = re.search(r"older_than_(\d+)_years", terms["roof_settlement"])
    return int(found.group(1)) if found else None


def wind_hail_rule(terms: dict[str, Any], region: str) -> tuple[str, Decimal | None, Decimal | None]:
    """The deductible wording for windstorm or hail in a region, as (text, percent, flat amount)."""
    rule = terms["wind_hail_deductible"]
    if rule["type"] == "percent_of_coverage_a" and region in rule["regions"]:
        return f"{rule['percent']}% of Coverage A", Decimal(rule["percent"]), None
    flat = Decimal(rule["elsewhere_flat"] if rule["type"] == "percent_of_coverage_a" else rule["amount"])
    return usd(flat), None, flat


def _schedule(terms: dict[str, Any]) -> str:
    perils: list[str] = policy()["perils"]
    limits = " | ".join(f"At Coverage A of {usd(limit)}" for limit in SCHEDULE_LIMITS)
    lines = [f"| Region | State | Peril | Deductible | {limits} |", "|" + " --- |" * (4 + len(SCHEDULE_LIMITS))]
    for region, states in policy()["regions"].items():
        for state in states.values():
            for peril in perils:
                if peril in ("wind", "hail"):
                    text, percent, flat = wind_hail_rule(terms, region)
                else:
                    flat = Decimal(terms["all_peril_deductible"])
                    text, percent = usd(flat), None
                amounts = [usd(limit * percent / 100) if percent else usd(flat or 0) for limit in SCHEDULE_LIMITS]
                lines.append(f"| {region} | {state} | {PERIL_NAMES[peril]} | {text} | {' | '.join(amounts)} |")
    return "\n".join(lines)


def wording(edition: str) -> Document:
    terms = policy()["editions"][edition]
    facts = policy()["facts"]
    start, end = terms["effective_from"], terms["effective_to"]
    all_peril = usd(terms["all_peril_deductible"])
    mold = usd(terms["mold_sublimit"])
    acv_age = roof_acv_age(terms)
    rule = terms["wind_hail_deductible"]
    region_names = spoken_list(
        f"{region} ({spoken_list(states.values())})" for region, states in policy()["regions"].items()
    )
    in_force = (
        f"This policy applies to loss that occurs on or after {long_date(start)}."
        if end is None
        else f"This policy applies to loss that occurs from {long_date(start)} through {long_date(end)}."
    )

    if rule["type"] == "percent_of_coverage_a":
        percent_regions = spoken_list(rule["regions"])
        wind_hail = (
            f"For loss caused by windstorm or hail to property located in the {percent_regions} regions, the"
            f" deductible is {rule['percent']}% of Coverage A, figured on the Coverage A limit shown in the"
            " Declarations. It replaces the all peril deductible for that loss. The percentage is applied to the"
            " Coverage A limit, not to the amount of the loss.\n\nFor loss caused by windstorm or hail to"
            f" property located in any other region, the deductible is {usd(rule['elsewhere_flat'])} per occurrence."
        )
    else:
        wind_hail = (
            f"For loss caused by windstorm or hail, the deductible is {usd(rule['amount'])} per occurrence in"
            " every region. No separate percentage deductible applies under this edition."
        )

    if acv_age is not None:
        roof = (
            f"Loss to a roof surface that is more than {acv_age} years old on the date of loss is settled at"
            " actual cash value. We determine the age of the roof surface from the date it was last replaced"
            " in full, as shown by permit, invoice or other records you provide.\n\nLoss to a roof surface"
            f" that is {acv_age} years old or less on the date of loss is settled at replacement cost, as for"
            " the rest of the dwelling."
        )
    else:
        roof = (
            "Loss to a roof surface is settled at replacement cost, as for the rest of the dwelling. The age"
            " of the roof surface does not change how the loss is settled under this edition."
        )

    body = f"""
# Homeowners Policy, Form {edition}

{in_force} The form is issued in {spoken_list(_all_states())}.

## Agreement

We will provide the insurance described in this policy in return for the premium and compliance with all
applicable provisions of this policy. The Declarations, this form and any endorsements make up the whole
policy.

## 1. Definitions

In this policy, "you" and "your" refer to the named insured shown in the Declarations and a spouse who lives
in the same household. "We", "us" and "our" refer to the company providing this insurance.

1.1 "Actual cash value" means the cost to repair or replace damaged property with new material of like kind
and quality, less a deduction for physical deterioration, depreciation and obsolescence.

1.2 "Coverage A limit" means the limit of liability for Coverage A, Dwelling, shown in the Declarations.

1.3 "Flood" means a general and temporary condition of partial or complete inundation of normally dry land
from the overflow of inland or tidal waters, from the unusual and rapid runoff of surface water from any
source, or from mudflow.

1.4 "Fungi" means any type or form of fungus, including mold, mildew and wet rot, and any spores, scents or
by-products produced or released by fungi.

1.5 "Occurrence" means one event, or a series of related events arising from the same cause, that results in
direct physical loss.

1.6 "Region" means the group of states in which the residence premises is located: {region_names}.

1.7 "Replacement cost" means the cost to repair or replace damaged property with material of like kind and
quality, without deduction for depreciation.

1.8 "Residence premises" means the one family dwelling where you reside, shown as the residence premises in
the Declarations, and the grounds and other structures at that location.

1.9 "Roof surface" means the shingles, tiles, metal panels or other covering of the roof, together with the
underlayment, flashing and vents.

## 2. Section I, Property Coverages

### 2.1 Coverage A, Dwelling

We cover the dwelling on the residence premises, including structures attached to the dwelling, and
materials and supplies on or next to the residence premises used to construct, alter or repair the
dwelling. The most we will pay for loss to the dwelling in one occurrence is the Coverage A limit.

### 2.2 Coverage B, Other Structures

We cover other structures on the residence premises that are set apart from the dwelling by clear space,
such as a detached garage, a shed or a fence, up to the Coverage B limit shown in the Declarations.

### 2.3 Coverage C, Personal Property

We cover personal property owned or used by an insured while it is anywhere in the world, up to the Coverage
C limit shown in the Declarations.

## 3. Perils Insured Against

We insure against direct physical loss to property described in Coverages A and B unless the loss is
excluded in Section 4. For property described in Coverage C, we insure against direct physical loss caused
by the following perils, subject to Section 4:

- Fire or lightning.
- Windstorm or hail. Loss to the interior of a building is covered only if the wind or hail first makes an
opening in a roof or wall.
- Theft, including attempted theft, and loss of property from a known place when it is likely the property
was stolen.
- Sudden and accidental discharge or overflow of water or steam from within a plumbing, heating, air
conditioning or automatic fire protective sprinkler system, or from a household appliance, including the
freezing of such a system.
- Fungi, mold or wet rot that results from a covered loss, subject to the sublimit in Section 9.

## 4. Exclusions

We do not insure for loss caused directly or indirectly by any of the following, regardless of any other
cause or event contributing at the same time or in any sequence to the loss.

### 4.1 Flood and surface water

{facts["flood_excluded"]}, surface water, waves, tidal water, overflow of a body of water or spray from any
of these, whether or not driven by wind. Water below the surface of the ground that seeps or leaks through a
foundation, wall, floor or paved surface is also excluded. This exclusion does not apply to water that
escapes from a plumbing system inside the dwelling.

### 4.2 Earth movement

Earthquake, landslide, mudflow, subsidence, sinkhole, or the sinking, rising, shifting, expanding or
contracting of earth, whether combined with water or not. Direct loss by fire or theft that follows earth
movement is covered.

### 4.3 Wear and tear

Wear and tear, marring, deterioration, mechanical breakdown, latent defect, rust, corrosion, smog, or the
settling, shrinking, bulging or expansion of pavements, foundations, walls, floors, roofs or ceilings.
Continuous or repeated seepage or leakage of water over a period of weeks or months is treated as wear and
tear.

### 4.4 Neglect

Neglect, meaning the failure of an insured to use all reasonable means to save and preserve property at and
after the time of a loss.

### 4.5 Fungi, mold and wet rot beyond the sublimit

Loss caused by fungi, mold or wet rot, except as provided in Section 9. We do not pay for any part of such a
loss that exceeds the sublimit in Section 9, and we do not pay at all when the fungi, mold or wet rot results
from an excluded cause, such as flood or wear and tear.

## 5. Deductibles

### 5.1 All peril deductible

Unless Section 5.2 applies, we pay only that part of the loss in one occurrence that exceeds {all_peril}.

### 5.2 Windstorm or hail deductible

{wind_hail}

### 5.3 Deductible schedule

The schedule below restates Sections 5.1 and 5.2 for each state in which this form is issued. The amounts
in the last two columns are examples for the Coverage A limits named in the column headings. The deductible
on a given policy is figured from the Coverage A limit in its own Declarations.

{_schedule(terms)}

### 5.4 One deductible per occurrence

When a single occurrence causes loss under more than one coverage, only the highest applicable deductible
applies. Hail on a roof and wind damage to a fence from the same storm are one occurrence.

## 6. Loss Settlement

### 6.1 Dwelling and other structures

Covered loss to the dwelling and other structures is settled at replacement cost, without deduction for
depreciation, subject to the following. Until the repair or replacement is complete, we pay no more than the
actual cash value of the damage. We pay the rest when the work is complete and you have sent us the final
invoice.

### 6.2 Roof surfaces

{roof}

### 6.3 Personal property

Covered loss to personal property is settled at actual cash value at the time of loss.

## 7. Duties After Loss

In case of a loss to covered property, we have no duty to provide coverage under this policy if the failure
to comply with the following duties is prejudicial to us. You must see that the following are done:

- Give us notice of the loss within {facts["notice_window"]} of the date of loss. A loss reported later than
that may be denied for late notice when the delay has prevented us from confirming the cause or the extent
of the damage.
- Notify the police in case of loss by theft.
- Protect the property from further damage. Make reasonable and necessary repairs to protect the property,
and keep an accurate record of the repair expenses.
- Prepare an inventory of damaged personal property showing the quantity, description, actual cash value
and amount of loss, with bills, receipts and related documents.
- As often as we reasonably require, show the damaged property and provide records and documents we request.
- Send us a signed, sworn proof of loss when we ask for one, setting out the time and cause of loss, the
interest of all insureds in the property, and the amount claimed.

## 8. Loss Payment

We will adjust all losses with you. We will pay you unless some other person is named in the policy or is
legally entitled to receive payment. Loss will be payable after we reach agreement with you, a court enters
a final judgment, or an appraisal award is filed with us.

## 9. Fungi, Mold and Wet Rot Sublimit

The most we will pay for all loss caused by fungi, mold or wet rot during one policy period is {mold},
regardless of the number of locations or claims. The sublimit includes the cost to remove the fungi, test
for them, and tear out and replace any part of the building needed to reach them. It is part of, and does
not increase, the Coverage A limit.
"""
    meta = {
        "doc_id": edition.lower(),
        "kind": "wording",
        "title": f"Homeowners Policy, Form {edition}",
        "ref": edition,
        "edition": edition,
        "effective_from": start,
        "effective_to": end,
    }
    return Document(f"wordings/{edition.lower()}.md", meta, _reflow(body))


def _reflow(body: str) -> str:
    # Paragraph text above is wrapped for the source file; the rendered file keeps one line per
    # paragraph or list item, which is how the parser reads it.
    out: list[str] = []
    for block in body.strip().split("\n\n"):
        lines = block.split("\n")
        if lines[0].startswith("|") or lines[0].startswith(("To:", "From:", "Date:", "Re:")):
            out.append(block)
            continue
        merged: list[str] = []
        for line in lines:
            if merged and not line.startswith(("- ", "#")) and not merged[-1].startswith("#"):
                merged[-1] += " " + line
            else:
                merged.append(line)
        out.append("\n".join(merged))
    return "\n\n".join(out) + "\n"


@dataclass(frozen=True)
class EventFacts:
    """Counts the memos and bulletins quote, taken from the seeded rows so a page agrees with the tables."""

    freeze_claims: Counter[str]
    freeze_prior_year: int
    hail_claims: int
    hail_april_baseline: int
    roofing_closed: int
    outage_payments: Counter[str]
    outage_claims: int
    outage_total: Decimal
    median_coverage: dict[str, Decimal]


def _event(event_id: str) -> dict[str, Any]:
    return next(e for e in events() if e["id"] == event_id)


def event_facts(data: Dataset) -> EventFacts:
    ref = policy()
    state_names = {code: name for states in ref["regions"].values() for code, name in states.items()}
    freeze, hail, roofing, outage = (
        _event(name)
        for name in ("pipe-freeze-2025-01", "hail-front-range-2025", "roofing-surge-2025q3", "payments-outage-2026-05")
    )

    def claims(region: str | None, state: str | None, perils: tuple[str, ...], first: date, last: date) -> list[Any]:
        return [
            c
            for c in data.claims
            if (region is None or c[2] == region) and (state is None or c[3] == state) and c[4] in perils
            and first <= c[5] <= last
        ]  # fmt: skip

    f_first, f_last = freeze["loss_dates"]
    in_freeze = claims(freeze["region"], None, (freeze["peril"],), f_first, f_last)
    prior = claims(freeze["region"], None, (freeze["peril"],), f_first.replace(year=2024), f_last.replace(year=2024))
    h_first, h_last = hail["loss_dates"]
    in_hail = claims(None, hail["state"], (hail["peril"],), h_first, h_last)
    april = claims(None, hail["state"], (hail["peril"],), date(2024, 4, 1), date(2024, 4, 30))
    r_first, r_last = roofing["closed_between"]
    closed = [
        c
        for c in data.claims
        if c[3] == roofing["state"] and c[4] in roofing["perils"] and c[8] == "closed" and r_first <= c[7] <= r_last
    ]
    o_first, o_last = outage["window"]
    voided = [p for p in data.payments if p[6] == "voided" and o_first <= p[3] <= o_last]
    by_state: dict[str, list[Decimal]] = {}
    for p in data.policies:
        by_state.setdefault(state_names[p[4]], []).append(p[5])
    return EventFacts(
        freeze_claims=Counter(state_names[c[3]] for c in in_freeze),
        freeze_prior_year=len(prior),
        hail_claims=len(in_hail),
        hail_april_baseline=len(april),
        roofing_closed=len(closed),
        outage_payments=Counter(p[5] for p in voided),
        outage_claims=len({p[1] for p in voided}),
        outage_total=sum((p[4] for p in voided), Decimal(0)),
        median_coverage={state: Decimal(median(values)) for state, values in by_state.items()},
    )


def _event_meta(event_id: str, issued: date) -> dict[str, Any]:
    doc = _event(event_id)["doc"]
    return {
        "doc_id": doc["ref"].lower(),
        "kind": doc["kind"],
        "title": doc["title"],
        "ref": doc["ref"],
        "issued": issued,
    }


def freeze_bulletin(facts: EventFacts) -> Document:
    event = _event("pipe-freeze-2025-01")
    first, last = event["loss_dates"]
    issued = date(2025, 2, 7)
    meta = _event_meta(event["id"], issued)
    states = _states(event["region"])
    counts = spoken_list(f"{facts.freeze_claims[s]} in {s}" for s in states)
    terms = policy()["editions"]["HO-2025"]
    body = f"""
# Catastrophe Bulletin {meta["ref"]}: {meta["title"]}

Issued {long_date(issued)} by Catastrophe Response. Applies to the {event["region"]} region ({spoken_list(states)}).

## Event

An arctic air mass held overnight temperatures well below zero across {spoken_list(states)} from
{span(first, last)}. Pipes froze and burst in unheated or poorly insulated parts of homes,
most often in exterior walls, crawl spaces and attached garages. Many insureds found the damage only when the
pipes thawed.

## Claim volume

As of {long_date(issued)} we have {sum(facts.freeze_claims.values())} water claims with a date of loss from
{span(first, last)}: {counts}. The same five days of January 2024 produced
{facts.freeze_prior_year}. Reporting has slowed, and we expect few further claims from this event.

## Coverage

A burst pipe is a sudden and accidental discharge of water from a plumbing system, which Section 3 of the
policy covers. These losses fall under Form HO-2025 and carry the all peril deductible of
{usd(terms["all_peril_deductible"])}. The percentage wind and hail deductible does not apply. Snowmelt that
entered at ground level is surface water and is excluded under Section 4.1.

## Handling

- Code every claim from this event {meta["ref"]} so that its losses can be reported apart from normal activity.
- Approve a mitigation vendor on first contact, following the water mitigation section of the claims handling
guidelines.
- Expect the fungi, mold and wet rot sublimit of {usd(terms["mold_sublimit"])} to come up on homes that sat
wet for several days before the loss was found.
"""
    return Document(f"bulletins/{meta['doc_id']}.md", meta, _reflow(body))


def deductible_memo(facts: EventFacts) -> Document:
    event = _event("deductible-edition-2025")
    issued = date(2024, 11, 15)
    meta = _event_meta(event["id"], issued)
    old, new = policy()["editions"]["HO-2023"], policy()["editions"]["HO-2025"]
    rule = new["wind_hail_deductible"]
    percent = Decimal(rule["percent"])
    flat_regions = spoken_list(r for r in policy()["regions"] if r not in rule["regions"])
    examples = [
        (state, facts.median_coverage[state])
        for region in rule["regions"]
        for state in _states(region)[:1]
    ]  # fmt: skip
    example_text = " ".join(
        f"On a home in {state} at the state's median Coverage A of {usd(limit)}, the deductible for a wind or"
        f" hail claim is now {usd(limit * percent / 100)}."
        for state, limit in examples
    )
    acv_age = roof_acv_age(new)
    body = f"""
# {meta["title"]}

To: Claims and underwriting staff, all regions
From: Personal Lines Underwriting
Date: {long_date(issued)}
Re: {meta["ref"]}, {meta["title"]}

Form HO-2025 replaces HO-2023 for losses on or after {long_date(new["effective_from"])}. The edition that
applies is set by the date of loss, not the date the claim is reported, so a 2024 loss reported in 2025 is
still handled under HO-2023.

In the {spoken_list(rule["regions"])} regions, the deductible for windstorm or hail becomes {percent}% of the
Coverage A limit instead of a flat {usd(old["wind_hail_deductible"]["amount"])}. {example_text} In the
{flat_regions} regions, wind and hail keep the {usd(rule["elsewhere_flat"])} deductible.

Two other terms change in every region. The fungi, mold and wet rot sublimit falls from
{usd(old["mold_sublimit"])} to {usd(new["mold_sublimit"])}, and a roof surface more than {acv_age} years old
on the date of loss is settled at actual cash value rather than replacement cost.

Expect fewer and larger wind and hail payments in the {spoken_list(rule["regions"])} regions from the spring
of 2025, and more of those claims closed without payment because the damage falls below the deductible.
Explain the deductible in dollars on first contact, so the insured is not surprised by the decision.
"""
    return Document(f"memos/{meta['doc_id']}.md", meta, _reflow(body))


def hail_bulletin(facts: EventFacts) -> Document:
    event = _event("hail-front-range-2025")
    first, last = event["loss_dates"]
    issued = date(2025, 5, 9)
    meta = _event_meta(event["id"], issued)
    state = policy()["regions"][event["region"]][event["state"]]
    terms = policy()["editions"]["HO-2025"]
    text, _, _ = wind_hail_rule(terms, event["region"])
    body = f"""
# Catastrophe Bulletin {meta["ref"]}: {meta["title"]}

Issued {long_date(issued)} by Catastrophe Response. Applies to the {event["region"]} region ({state}).

## Event

Two days of severe thunderstorms crossed the Front Range from Fort Collins to Colorado Springs on
{span(first, last, "and")}, with hail up to two inches across the Denver metro area. Roofs,
gutters, siding and window wraps took most of the damage.

## Claim volume

As of {long_date(issued)} we have {facts.hail_claims} hail claims in {state} with a date of loss of
{span(first, last, "or")}. For comparison, {state} produced {facts.hail_april_baseline}
hail claims in the whole of April 2024.

## Coverage and deductible

These losses fall under Form HO-2025. In {state} the windstorm or hail deductible is {text}, figured on the
Coverage A limit in the Declarations. Roofs with light damage will often fall below it, and those claims are
closed without payment after inspection. A roof surface more than {roof_acv_age(terms)} years old on the date
of loss is settled at actual cash value.

## Handling

- Inspect under the hail inspection procedure in the claims handling guidelines, with a test square on every
slope.
- Confirm the date of loss against the storm dates above. Damage that does not match this storm belongs to an
earlier loss.
- Code every claim from this event {meta["ref"]}.
- Contractors are booked for months. Expect most payments on these claims to go out from May through July.
"""
    return Document(f"bulletins/{meta['doc_id']}.md", meta, _reflow(body))


def roofing_memo(facts: EventFacts) -> Document:
    event = _event("roofing-surge-2025q3")
    first, last = event["closed_between"]
    issued = date(2025, 10, 15)
    meta = _event_meta(event["id"], issued)
    state = policy()["regions"][event["region"]][event["state"]]
    increase = round((Decimal(str(event["severity_multiplier"])) - 1) * 100)
    body = f"""
# {meta["title"]}

To: {event["region"]} region claims staff
From: Claims Cost Management
Date: {long_date(issued)}
Re: {meta["ref"]}, {meta["title"]}

Roofing contractors in the Denver metro area raised their prices by about {increase} percent for work billed
from {span(first, last)}. The increase covers labor and materials alike. Crews are still
committed to repairs from the April hail storm (CAT-25-07), and shingle and underlayment prices rose over the
same months.

We reviewed the {state} hail and wind claims closed in the quarter, {facts.roofing_closed} in all. Their repair
costs run about {increase} percent above what the same scope of work cost before July. The change is in the
price of the work, not in the damage: the number of roofing squares and the items on the estimates are in line
with earlier claims.

Do not question estimates that reflect the new prices. Reserve open {state} roof claims at current prices, and
use the price list dated {long_date(first)} in the estimating system. We will review again at the end of the
fourth quarter.
"""
    return Document(f"memos/{meta['doc_id']}.md", meta, _reflow(body))


def outage_memo(facts: EventFacts) -> Document:
    event = _event("payments-outage-2026-05")
    first, last = event["window"]
    re_first, re_last = event["reissue_between"]
    issued = date(2026, 6, 8)
    meta = _event_meta(event["id"], issued)
    count = sum(facts.outage_payments.values())
    body = f"""
# {meta["title"]}

To: All claims staff
From: Claims Operations
Date: {long_date(issued)}
Re: {meta["ref"]}, {meta["title"]}

The payments platform failed to deliver claim payments issued from {span(first, last)}.
The {count} payments issued in that window, on {facts.outage_claims} claims and totaling
{usd(facts.outage_total, cents=True)}, were voided. Of these, {facts.outage_payments["indemnity"]} were indemnity
payments and {facts.outage_payments["expense"]} were expense payments.

Each voided payment was reissued for the same amount to the same payee from {span(re_first, re_last)}.
No claim was paid twice and none was paid less. Payees received their money two to three weeks late.

The voids and reissues distort paid-loss reports by payment date. A report that counts voided payments shows
money in May that was never delivered, and one that ignores reissues misses it in late May and early June.
Count reissued payments and exclude voided ones, which is how the reporting views are built.

If an insured or a vendor asks about a late payment from this period, confirm the reissue date on the claim
and apologize for the delay. Section 6 of the claims handling guidelines covers how a voided payment is
reissued.
"""
    return Document(f"memos/{meta['doc_id']}.md", meta, _reflow(body))


def guidelines() -> Document:
    old, new = policy()["editions"]["HO-2023"], policy()["editions"]["HO-2025"]
    rule = new["wind_hail_deductible"]
    percent = Decimal(rule["percent"])
    example = SCHEDULE_LIMITS[1]
    extra = example * percent / 100 - Decimal(new["all_peril_deductible"])
    outage = _event("payments-outage-2026-05")
    re_first, re_last = outage["reissue_between"]
    notice = policy()["facts"]["notice_window"]
    mold = f"{usd(old['mold_sublimit'])} under HO-2023 and {usd(new['mold_sublimit'])} under HO-2025"
    issued = date(2026, 6, 15)
    meta = {
        "doc_id": "claims-guidelines",
        "kind": "guideline",
        "title": "Property Claims Handling Guidelines",
        "ref": "CG-2026",
        "issued": issued,
    }
    body = f"""
# Property Claims Handling Guidelines

Revised {long_date(issued)}. These guidelines apply to homeowners claims under Forms HO-2023 and
HO-2025 in every region. Where a guideline and the policy wording disagree, the wording in force on the date of
loss controls.

## 1. Reserving

1.1 Set the initial indemnity reserve within two business days of first contact, at your best estimate of what
we will pay: the covered damage, less the deductible in force on the date of loss, capped by any sublimit.

1.2 Wind and hail losses in the {spoken_list(rule["regions"])} regions on or after
{long_date(new["effective_from"])} carry the HO-2025 deductible of {percent}% of Coverage A. Net that amount
from the reserve, not {usd(new["all_peril_deductible"])}. On a dwelling insured for {usd(example)} the
difference is {usd(extra)}.

1.3 A mold reserve never exceeds the sublimit of the edition in force: {mold}.

1.4 Review the reserve at every material change, such as a new estimate, a supplement or a coverage decision,
and at least every 30 days while the claim is open. Record the reason for each change in a file note.

1.5 A reserve above your authority goes to your supervisor before it is posted. Adjuster authority is $25,000
per claim.

## 2. Water mitigation

2.1 On first contact for any water loss, confirm that the source has been stopped and tell the insured to begin
drying out the home. Drying that starts within 48 hours prevents most mold.

2.2 Approve a mitigation vendor on first contact when standing water or wet drywall is reported. Ask the vendor
for moisture readings on arrival and at completion, with photos.

2.3 A frozen or burst pipe is a sudden discharge from a plumbing system and is covered. Water that enters from
outside at ground level, or seeps through a foundation, is excluded as flood or surface water. Record which it
is and how you know.

2.4 Mold found during mitigation is paid only up to the sublimit, {mold}, including testing and tear-out. Tell
the insured about the sublimit in writing when mold is first found.

2.5 Continuous seepage over weeks or months is wear and tear, not a sudden discharge. Look for staining, rot and
mineral deposits that show a long-running leak.

## 3. Hail inspection procedure

3.1 Before the inspection, confirm from a hail verification report that hail fell at the property on the
reported date of loss, and note the reported hail size.

3.2 Inspect every roof slope. On each slope, mark a 10 by 10 foot test square and count the functional hail hits
in it: bruised or fractured shingles, granule loss down to the mat, or punctures. Cosmetic dents in metal are
not functional damage.

3.3 Eight or more functional hits in a test square support replacing that slope. Fewer than eight support
repairing the hits found. Photograph each test square with its chalk outline and hit count.

3.4 Check the soft metals, such as gutters, downspouts, vents and window wraps, for spatter and dents that agree
with the reported hail size. Damage that does not agree with the storm on record points to an earlier loss.

3.5 Record the age of the roof surface from permits or invoices. Under HO-2025, a roof surface more than
{roof_acv_age(new)} years old on the date of loss is settled at actual cash value.

3.6 Compare the estimate with the deductible before writing it up. Under HO-2025 in the
{spoken_list(rule["regions"])} regions, many hail losses fall below {percent}% of Coverage A.

## 4. Denial letters

4.1 A denial letter names the policy form and the edition in force on the date of loss, quotes the provision
relied on with its section number, states the facts that led to the decision, and tells the insured how to ask
for a review.

4.2 The usual grounds in this book are a loss below the deductible, an excluded cause such as flood or surface
water, and late notice, where the loss was reported more than {notice} after the date of loss.

4.3 Late notice alone is not enough. The letter must say what the delay kept us from confirming, such as the
cause of the loss or the extent of the damage.

4.4 Send the letter within five business days of the decision, and record the date it was sent in a file note.

4.5 A supervisor reviews every denial for late notice, and every denial where the damage claimed is more than
$25,000, before the letter goes out.

## 5. Escalation to a supervisor

Escalate to your supervisor, and record the escalation in a file note, when any of the following applies.

- The reserve would exceed your authority.
- Coverage depends on a disputed fact, such as the cause of a water loss or the age of a roof.
- You suspect fraud, or the insured's account of the loss changes in a material way.
- An attorney or a public adjuster becomes involved, or a complaint is filed with a state department of
insurance.
- Anyone other than the insured asks for information about the claim, or a caller asks about a claim outside
your region.

Supervisors hold authority for reserves, payments and denials in every region.

## 6. Reissuing voided payments

6.1 A payment that was issued but never delivered is voided, and the same amount is reissued to the same payee.
A reissue is not a new payment, and the reserve does not change because of it.

6.2 Record the void and the reissue on the claim, with the reference of the memo or incident that caused them.

6.3 Paid-loss figures count the reissued payment and exclude the voided one, so a void and its reissue together
never change the amount paid on a claim.

6.4 Payments voided during the payments platform outage of May 2026 were reissued from
{span(re_first, re_last)}, as set out in {outage["doc"]["ref"]}.
"""
    return Document("guidelines/claims-guidelines.md", meta, _reflow(body))


def corpus(data: Dataset | None = None) -> list[Document]:
    """The committed documents. Counts come from the rows as of the date in policy.yaml, whatever AS_OF says."""
    facts = event_facts(data or rows.generate(as_of=policy()["as_of"]))
    return [
        *(wording(edition) for edition in policy()["editions"]),
        guidelines(),
        freeze_bulletin(facts),
        deductible_memo(facts),
        hail_bulletin(facts),
        roofing_memo(facts),
        outage_memo(facts),
    ]


def stale_files(documents: list[Document], root: Path = CORPUS_DIR) -> list[str]:
    """Paths under root that differ from what the generator renders now, including files it no longer renders."""
    expected = {doc.path: doc.markdown() for doc in documents}
    present = {str(path.relative_to(root)) for path in root.rglob("*.md")} if root.exists() else set()
    stale = [path for path, text in expected.items() if not (root / path).exists() or (root / path).read_text() != text]
    return sorted(stale + [path for path in present - expected.keys()])


def write_corpus(documents: list[Document], root: Path = CORPUS_DIR) -> list[str]:
    changed = stale_files(documents, root)
    expected = {doc.path: doc.markdown() for doc in documents}
    for path in changed:
        target = root / path
        if path in expected:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expected[path])
        else:
            target.unlink()
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the policy wordings, guidelines, memos and bulletins.")
    parser.add_argument("--check", action="store_true", help="exit non-zero if data/corpus is out of date")
    args = parser.parse_args()
    documents = corpus()
    if args.check:
        stale = stale_files(documents)
        if stale:
            raise SystemExit(f"data/corpus is out of date: {', '.join(stale)}")
        print(f"{len(documents)} documents, data/corpus current")
        return
    changed = write_corpus(documents)
    print(
        f"{len(documents)} documents, {len(changed)} written or removed under {CORPUS_DIR.relative_to(DATA_DIR.parent)}"
    )


if __name__ == "__main__":
    main()
