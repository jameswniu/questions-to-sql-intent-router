import re
from dataclasses import replace

from app.semantic.cues import NEGATION, Scan, compare_cues, grouping, unknown_qualifiers
from app.semantic.layer import Layer, Measure, Phrase
from app.semantic.query import Clarify, Compare, MetricQuery, Period
from app.semantic.timephrase import Clock, find_periods

FOLLOW_UP = re.compile(
    r"^\s*(?:and|what\s+about|how\s+about|what\s+of|same\s+(?:for|but|with)|now|also|ok(?:ay)?,?\s+(?:and|now))\b",
    re.IGNORECASE,
)
YEARLESS_QUARTER = re.compile(r"\b(?:q([1-4])|(first|second|third|fourth)\s+quarter)\b", re.IGNORECASE)
ORDINAL = {"first": 1, "second": 2, "third": 3, "fourth": 4}


def _pattern(phrase: Phrase) -> re.Pattern[str]:
    words = r"[\s-]+".join(re.escape(w) for w in phrase.text.split())
    if phrase.case_sensitive:
        return re.compile(rf"(?<!\w){words}(?!\w)")
    return re.compile(rf"(?<!\w){words}(?:e?s)?(?!\w)", re.IGNORECASE)


def _match_vocabulary(scan: Scan, layer: Layer) -> tuple[dict[str, int], dict[str, list[str]]]:
    measures: dict[str, int] = {}
    wanted: dict[str, list[str]] = {}
    excluded: dict[str, list[str]] = {}
    for phrase in layer.phrases:
        for m in _pattern(phrase).finditer(scan.text):
            if not scan.free(m.start(), m.end()):
                continue
            scan.claim(m.start(), m.end())
            if phrase.target[0] == "measure":
                measures[phrase.target[1]] = max(measures.get(phrase.target[1], 0), len(phrase.text))
                continue
            _, dim, value = phrase.target
            bucket = excluded if NEGATION.search(scan.text[: m.start()]) else wanted
            if value not in bucket.setdefault(dim, []):
                bucket[dim].append(value)
    for dim, values in excluded.items():
        if dim not in wanted:
            wanted[dim] = [v for v in layer.dimensions[dim].values if v not in values]
    return measures, wanted


def _fallback_measure(layer: Layer) -> str | None:
    """The measure the bare word 'claims' names; any more specific measure in the question outranks it."""
    for phrase in layer.phrases:
        if phrase.text == "claims" and phrase.target[0] == "measure":
            return phrase.target[1]
    return None


def _option(measure: Measure) -> str:
    return next((s for s in measure.synonyms if " " in s), measure.label)


def _pick_measure(hits: dict[str, int], layer: Layer) -> str | Clarify | None:
    fallback = _fallback_measure(layer)
    if len(hits) > 1 and fallback in hits:
        del hits[fallback]
    if not hits:
        return None
    best = max(hits.values())
    winners = [name for name, size in hits.items() if size == best]
    if len(winners) == 1:
        return winners[0]
    options = tuple(_option(layer.measures[name]) for name in winners[:3])
    return Clarify("measure", f"Which do you mean: {', '.join(options[:-1])} or {options[-1]}?", options)


def _relation(current: Period, other: Period) -> Compare | None:
    earlier = current.years_earlier()
    if other.start <= earlier.start and earlier.end <= other.end:
        return "prior_year"
    if other == current.prior():
        return "prior_period"
    return None


def _periods(scan: Scan, clock: Clock, compare: Compare | None) -> tuple[Period | None, Compare | None, Clarify | None]:
    found = [f for f in find_periods(scan.text, clock) if scan.free(f.start, f.end)]
    for f in found:
        scan.claim(f.start, f.end)
    if len(found) <= 1:
        return (found[0].period if found else None), compare, None
    ordered = sorted((f.period for f in found), key=lambda p: p.start, reverse=True)
    relation = _relation(ordered[0], ordered[1]) if len(ordered) == 2 else None
    if relation is None:
        options = tuple(p.label for p in ordered[:3])
        return None, None, Clarify("period", "Which period should I use?", options)
    return ordered[0], relation, None


def _yearless(scan: Scan, previous: MetricQuery | None, clock: Clock) -> Period | Clarify | None:
    m = next((m for m in YEARLESS_QUARTER.finditer(scan.text) if scan.free(m.start(), m.end())), None)
    if m is None:
        return None
    scan.claim(m.start(), m.end())
    quarter = int(m[1]) if m[1] else ORDINAL[m[2].lower()]
    if previous is not None and previous.period is not None:
        return Period.quarter(previous.period.start.year, quarter)
    years = range(clock.data_end.year, clock.data_end.year - 3, -1)
    options = tuple(Period.quarter(y, quarter).label for y in years if Period.quarter(y, quarter).end <= clock.data_end)
    return Clarify("period", f"Q{quarter} of which year?", options[:3])


def period_options(clock: Clock) -> tuple[str, ...]:
    return (str(clock.as_of.year - 1), clock.current_quarter.prior().label, "year to date")


def extract(question: str, layer: Layer, previous: MetricQuery | None = None) -> MetricQuery | Clarify:
    clock = Clock(layer.as_of, layer.coverage.end)
    scan = Scan(question)
    compare, versus = compare_cues(scan)
    period, compare, ambiguous = _periods(scan, clock, compare)
    if ambiguous:
        return ambiguous
    if period is None:
        yearless = _yearless(scan, previous, clock)
        if isinstance(yearless, Clarify):
            return yearless
        period = yearless
    if versus and compare is None and period is not None:
        compare = "prior_year"
    group_by, grain, limit = grouping(scan)
    hits, filters = _match_vocabulary(scan, layer)
    picked = _pick_measure(hits, layer)
    if isinstance(picked, Clarify):
        return picked
    unknown = unknown_qualifiers(scan)
    follow_up = previous is not None and (picked is None or FOLLOW_UP.match(question) is not None)
    if follow_up and previous is not None:
        carried = dict(previous.filters)
        if "region" in filters and "state" not in filters:
            carried.pop("state", None)
        if "state" in filters and "region" not in filters:
            carried.pop("region", None)
        carried.update({dim: tuple(values) for dim, values in filters.items()})
        return replace(
            previous,
            measure=picked or previous.measure,
            filters=carried,
            group_by=tuple(group_by) or previous.group_by,
            period=period or previous.period,
            compare_to=compare or previous.compare_to,
            grain=grain or previous.grain,
            limit=limit or previous.limit,
            unknown=unknown,
            notes=(),
        )
    if picked is None:
        likely = [name for name in ("claim_count", "paid_losses", "denial_rate") if name in layer.measures]
        options = tuple(_option(layer.measures[name]) for name in likely or list(layer.measures)[:3])
        return Clarify("measure", f"Which figure do you want: {', '.join(options[:-1])} or {options[-1]}?", options)
    query = MetricQuery(
        measure=picked,
        filters={dim: tuple(values) for dim, values in filters.items()},
        group_by=tuple(group_by),
        period=period,
        compare_to=compare,
        grain=grain,
        limit=limit,
        unknown=unknown,
    )
    if period is None and not layer.measures[picked].point_in_time:
        label = layer.measures[picked].label
        return Clarify("period", f"For which period do you want {label}?", period_options(clock), query)
    return query
