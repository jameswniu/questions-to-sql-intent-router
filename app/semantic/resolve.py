import difflib
from dataclasses import replace

from app.semantic.describe import display_name
from app.semantic.extract import period_options
from app.semantic.layer import Layer, Measure
from app.semantic.query import GRAINS, Clarify, MetricQuery, OutOfData, Period, month_end
from app.semantic.timephrase import Clock

MAX_LIMIT = 50


def _join(words: list[str], last: str = "or") -> str:
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} {last} {words[-1]}"


def near_values(word: str, layer: Layer, dimension: str | None = None, n: int = 3) -> tuple[str, ...]:
    """Close matches from the layer's own vocabulary, so a suggestion never echoes what the database holds."""
    table: dict[str, str] = {}
    for phrase in layer.phrases:
        if phrase.target[0] == "value" and dimension in (None, phrase.target[1]):
            table.setdefault(phrase.text.lower(), phrase.target[2])
    close = difflib.get_close_matches(word.lower(), list(table), n=n * 2, cutoff=0.6)
    if not close:
        scored = sorted(table, key=lambda t: (-difflib.SequenceMatcher(None, word.lower(), t).ratio(), t))
        close = scored[: n * 2]
    return tuple(dict.fromkeys(table[t] for t in close))[:n]


def _canonical(value: str, allowed: tuple[str, ...]) -> str | None:
    return next((v for v in allowed if v.lower() == value.lower()), None)


def _check_values(mq: MetricQuery, layer: Layer) -> MetricQuery | Clarify:
    if mq.unknown:
        word = mq.unknown[0]
        options = near_values(word, layer)
        question = f'I don\'t have anything called "{word}". Did you mean {_join(list(options))}?'
        return Clarify("value", question, options, replace(mq, unknown=()))
    filters: dict[str, tuple[str, ...]] = {}
    for dim, values in mq.filters.items():
        known = layer.dimensions.get(dim)
        if known is None or known.is_grain:
            return Clarify("dimension", f'I can\'t filter by "{dim}".', _filterable(layer))
        fixed = []
        for value in values:
            canonical = _canonical(value, known.values)
            if canonical is None:
                options = near_values(value, layer, dim)
                question = f'There is no {dim} called "{value}". Did you mean {_join(list(options))}?'
                rest = {d: v for d, v in mq.filters.items() if d != dim}
                return Clarify("value", question, options, replace(mq, filters=rest))
            fixed.append(canonical)
        filters[dim] = tuple(dict.fromkeys(fixed))
    return replace(mq, filters=filters)


def _filterable(layer: Layer) -> tuple[str, ...]:
    return tuple(d.name for d in layer.dimensions.values() if not d.is_grain)


def _check_dimensions(mq: MetricQuery, measure: Measure, layer: Layer, analyst: bool) -> Clarify | None:
    allowed = layer.dimensions_for(measure, analyst=analyst)
    asked = [*mq.filters, *mq.group_by, *([mq.grain] if mq.grain else [])]
    unknown = [d for d in mq.group_by if d not in layer.dimensions]
    if unknown:
        return Clarify("dimension", f'I can\'t group by "{unknown[0]}".', allowed)
    blocked = [d for d in asked if d not in allowed]
    if not blocked:
        return None
    kept = replace(
        mq,
        filters={d: v for d, v in mq.filters.items() if d in allowed},
        group_by=tuple(d for d in mq.group_by if d in allowed),
        grain=mq.grain if mq.grain in allowed else None,
    )
    columns = [d for d in allowed if d not in GRAINS]
    times = "" if measure.point_in_time else " or by month, quarter or year"
    if not columns and not times:
        return Clarify("dimension", f"I can't split {display_name(measure)} any further.", (), kept)
    listed = f" by {_join(columns)}" if columns else ""
    return Clarify(
        "dimension",
        f"I can't break {display_name(measure)} down by {blocked[0]}. I can split it{listed}{times}.",
        allowed,
        kept,
    )


def whole_months(period: Period) -> Period:
    start = period.start.replace(day=1)
    return Period.between(start, month_end(period.end.year, period.end.month))


def _check_period(mq: MetricQuery, measure: Measure, layer: Layer, analyst: bool) -> MetricQuery | OutOfData:
    coverage = layer.coverage
    period = mq.period
    assert period is not None
    clipped = period.clip(coverage.start, coverage.end)
    if clipped is None:
        return OutOfData(f"My data covers {coverage.label}, so I can't answer for {period.label}.", coverage)
    if clipped != period:
        mq = mq.with_note(f"My data covers {coverage.label}, so this is {clipped.label}.")
    monthly = analyst or any(source == "sem.v_premium" for source in measure.sources)
    if monthly and clipped.months is None:
        aligned = whole_months(clipped).clip(coverage.start, coverage.end)
        assert aligned is not None
        mq = mq.with_note(f"This figure is kept by whole months, so it covers {aligned.label}.")
        clipped = aligned
    mq = replace(mq, period=clipped)
    if mq.compare_to is not None:
        prior = clipped.years_earlier() if mq.compare_to == "prior_year" else clipped.prior()
        if prior.clip(coverage.start, coverage.end) != prior:
            return OutOfData(
                f"My data covers {coverage.label}, so I can't compare {clipped.label} with {prior.label}.", coverage
            )
    return mq


def resolve(mq: MetricQuery, layer: Layer, *, analyst: bool = False) -> MetricQuery | Clarify | OutOfData:
    measure = layer.measures.get(mq.measure)
    if measure is None:
        options = tuple(list(layer.measures)[:3])
        return Clarify("measure", f'I don\'t have a figure called "{mq.measure}".', options)
    checked = _check_values(mq, layer)
    if isinstance(checked, Clarify):
        return checked
    mq = checked
    if (clarify := _check_dimensions(mq, measure, layer, analyst)) is not None:
        return clarify
    if mq.limit is not None:
        mq = replace(mq, limit=max(1, min(mq.limit, MAX_LIMIT)))
    if measure.point_in_time:
        if mq.period is not None or mq.compare_to is not None:
            mq = replace(mq, period=None, compare_to=None).with_note(
                f"{measure.label.capitalize()} are a snapshot at the data date, so I left out the period."
            )
        return mq
    if mq.period is None:
        options = period_options(Clock(layer.as_of, layer.coverage.end))
        return Clarify("period", f"For which period do you want {measure.label}?", options, mq)
    if mq.compare_to is not None and mq.grain is not None:
        label = mq.period.label
        return Clarify(
            "compare",
            f"I can compare totals or groups, but not a {mq.grain}ly series. Which would you like?",
            (f"{measure.label} by {mq.grain} in {label}", f"{measure.label} in {label} versus the year before"),
        )
    return _check_period(mq, measure, layer, analyst)
