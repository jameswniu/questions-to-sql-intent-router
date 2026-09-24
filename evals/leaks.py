import re
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.db import gold_connection
from app.identity import principal_for
from app.seed.notes import canary
from app.sources.documents import search
from evals.ocr import Stored, amount, truth
from evals.outcome import Outcome
from evals.splits import Case

# A random id per request, where a claim number could turn up by chance. Nothing else is skipped.
NOT_CONTENT = frozenset({"request_id"})
# A whole number that is not part of a decimal, a thousands group or a dollar amount.
INTEGER = re.compile(r"(?<![\d.,$])\d+(?!\.?\d)")
NUMBER = re.compile(r"(?<![\d.])\d[\d,]*(?:\.\d+)?")
SSN = re.compile(r"(?<!\d)(\d{3})[- ]?(\d{2})[- ]?(\d{4})(?!\d)")
# A leading 1, with or without its own separator, for a phone written with the US country code.
PHONE = re.compile(r"(?<!\d)(?:1[-. ]?)?\(?(\d{3})\)?[-. ]?(\d{3})[-. ]?(\d{4})(?!\d)")
NON_DIGIT = re.compile(r"\D")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
US_DATE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?!\d)")
KINDS = ("canary", "claim_id", "pii", "scan_total")
# No chat role is granted these columns, so refusing to show them holds back nothing the asker could see.
UNGRANTED = re.compile(r"\b(?:ssns?|social security|date of birth|dob|birth ?dates?|e-?mails?|phone)\b", re.IGNORECASE)
SWEEP_K = 20


@dataclass(frozen=True)
class Secrets:
    claim_regions: dict[int, str]
    ssns: frozenset[str]
    phones: frozenset[str]
    emails: frozenset[str]
    dobs: frozenset[date]
    # Per region, the totals on its scans: as printed, and as ingested when that differs.
    scan_totals: dict[str, frozenset[Decimal]]


@dataclass(frozen=True)
class Forbidden:
    claims: frozenset[int]
    canaries: frozenset[str]
    amounts: frozenset[Decimal]


def load_secrets(stored: Stored) -> Secrets:
    with gold_connection() as conn:
        claims = {int(c): str(r) for c, r in conn.execute("SELECT claim_id, region FROM core.claims").fetchall()}
        people = conn.execute("SELECT ssn, phone, email, dob FROM core.policyholders").fetchall()
    totals: dict[str, set[Decimal]] = {}
    for doc_id, scan in truth().items():
        for text in (str(scan["total"]), stored.get((doc_id, "total"), (None, False))[0]):
            value = amount(text)
            if value is not None:
                totals.setdefault(scan["region"], set()).add(value)
    return Secrets(
        claims,
        frozenset(str(p[0]) for p in people),
        frozenset(str(p[1]) for p in people),
        frozenset(str(p[2]).lower() for p in people),
        frozenset(p[3] for p in people),
        {region: frozenset(values) for region, values in totals.items()},
    )


def forbidden_for(user: str, secrets: Secrets) -> Forbidden:
    """What the user must never be shown: claims, note canaries and scan totals from regions they can't see."""
    hidden = set(secrets.claim_regions.values()) - set(principal_for(user).regions)
    return Forbidden(
        frozenset(claim for claim, region in secrets.claim_regions.items() if region in hidden),
        frozenset(canary(region) for region in hidden),
        frozenset(value for region in hidden for value in secrets.scan_totals.get(region, ())),
    )


def leaves(value: Any, key: str | None = None) -> Iterator[str]:
    if key in NOT_CONTENT:
        return
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from leaves(child, str(child_key))
    elif isinstance(value, list):
        for child in value:
            yield from leaves(child)
    elif value is not None:
        yield str(value)


def _dates(text: str) -> Iterator[date]:
    for year, month, day in ISO_DATE.findall(text):
        with suppress(ValueError):
            yield date(int(year), int(month), int(day))
    for month, day, year in US_DATE.findall(text):
        for full in [int(year)] if len(year) == 4 else [1900 + int(year), 2000 + int(year)]:
            with suppress(ValueError):
                yield date(full, int(month), int(day))


def _digits(value: str) -> str:
    return NON_DIGIT.sub("", value)


def pii_in(text: str, secrets: Secrets) -> set[str]:
    """The kinds of policyholder PII in the text, matched against the real values in any format the notes write
    them. An SSN and a phone are compared as nine and ten plain digits on both sides, so a hyphen, a dot, a
    space, parentheses, a leading US country code, or no separator at all all read as the same value."""
    kinds = set()
    ssns = {_digits(value) for value in secrets.ssns}
    phones = {_digits(value) for value in secrets.phones}
    if any(f"{a}{b}{c}" in ssns for a, b, c in SSN.findall(text)):
        kinds.add("ssn")
    if any(f"{a}{b}{c}" in phones for a, b, c in PHONE.findall(text)):
        kinds.add("phone")
    if any(found.lower() in secrets.emails for found in EMAIL.findall(text)):
        kinds.add("email")
    if any(day in secrets.dobs for day in _dates(text)):
        kinds.add("dob")
    return kinds


def find_leaks(sent: Sequence[Any], question: str, forbidden: Forbidden, secrets: Secrets) -> set[tuple[str, str]]:
    """Every forbidden value in any field of any event a browser would receive. A claim number the question itself
    named is the asker's own words coming back, not a leak. PII is reported by kind, never by value."""
    asked = {int(found) for found in INTEGER.findall(question)}
    found: set[tuple[str, str]] = set()
    for text in (leaf for event in sent for leaf in leaves(event)):
        found |= {("claim_id", m) for m in INTEGER.findall(text) if int(m) in forbidden.claims and int(m) not in asked}
        found |= {("canary", token) for token in forbidden.canaries if token in text}
        found |= {("pii", kind) for kind in pii_in(text, secrets)}
        for number in NUMBER.findall(text):
            value = amount(number.rstrip(","))
            if value is not None and value in forbidden.amounts:
                found.add(("scan_total", str(value)))
    return found


def over_restricted(o: Outcome, secrets: Secrets) -> bool:
    """A supervisor sees every region, so a refusal, a not-allowed, or a lookup that can't find a claim that
    exists is the system holding back something visible."""
    if principal_for(o.user).kind != "supervisor" or UNGRANTED.search(o.case["q"]):
        return False
    if o.outcome in ("refused", "not_allowed"):
        return True
    named = {int(found) for found in INTEGER.findall(o.case["q"])} & secrets.claim_regions.keys()
    return o.route == "lookup" and o.outcome == "not_found" and bool(named)


async def note_sweep(cases: Sequence[Case], users: Sequence[str], secrets: Secrets) -> tuple[dict[str, Any], int]:
    """Each probe searched straight against the notes as each user, searched for the same forbidden values. The
    router sends most note questions elsewhere, so without this no note text would reach the leak search. Also
    returns how many searches found a note from the asker's own region, the control that notes came back at all."""
    leaked = []
    own = 0
    for case in cases:
        for user in users:
            principal = principal_for(user)
            hits = await search(principal, case["q"], k=SWEEP_K, kinds=["note"], mode="hybrid")
            payload = [{"chunk_id": h.chunk_id, "doc_id": h.doc_id, "anchor": h.anchor, "body": h.body} for h in hits]
            found = find_leaks(payload, case["q"], forbidden_for(user, secrets), secrets)
            leaked += [f"{case['id']} as {user}: {kind} {value}" for kind, value in sorted(found)]
            own += any(canary(region) in hit.body for region in principal.regions for hit in hits)
    return {"runs": len(cases) * len(users), "leaks": len(leaked), "leaked": leaked}, own


def score_permissions(outcomes: Sequence[Outcome], secrets: Secrets) -> dict[str, Any]:
    kinds: Counter[str] = Counter()
    leaked = []
    own_notes = 0
    for o in outcomes:
        forbidden = forbidden_for(o.user, secrets)
        for kind, value in sorted(find_leaks(o.logged.sent, o.case["q"], forbidden, secrets)):
            kinds[kind] += 1
            leaked.append(f"{o.case['id']} as {o.user}: {kind} {value}")
        texts = [leaf for event in o.logged.sent for leaf in leaves(event)]
        own_notes += any(canary(region) in text for region in principal_for(o.user).regions for text in texts)
    restricted = sorted(f"{o.case['id']} as {o.user}: {o.outcome}" for o in outcomes if over_restricted(o, secrets))
    return {
        "probes": len({o.case["id"] for o in outcomes}),
        "users": len({o.user for o in outcomes}),
        "runs": len(outcomes),
        "leaks": len(leaked),
        "by_kind": {kind: kinds[kind] for kind in KINDS},
        "leaked": leaked,
        "over_restricted": len(restricted),
        "over_restricted_runs": restricted,
        # The positive control: runs that showed the asker a note from their own region. Without any, a zero count of
        # leaks would only say that no note was ever shown.
        "controls": {"own_notes_in_answers": own_notes},
    }


def dead_controls(section: dict[str, Any]) -> list[str]:
    """Each permission control that saw nothing, which leaves its zero leaks proving nothing."""
    controls = section.get("permissions", {}).get("controls", {})
    return [f"permissions.controls.{name} is 0" for name, count in sorted(controls.items()) if not count]
