import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, cast

from app.live.errors import Invalid
from app.semantic.layer import Layer
from app.semantic.query import COMPARES, GRAINS, Compare, Grain, MetricQuery, Period

# A MetricQuery as a model writes it: every field present, "none" or empty where the question says nothing.
NONE = "none"
FIELDS = ("measure", "filters", "group_by", "period", "compare_to", "grain", "limit")
RANGE = re.compile(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})")
PERIOD_FORMS = (
    "2025 for a year, 2025-Q2 for a quarter, 2025-01 for a month, 2025-01-01..2025-06-30 for any other span, "
    "or an empty string when the question names no period"
)


def filterable(layer: Layer) -> tuple[str, ...]:
    return tuple(d.name for d in layer.dimensions.values() if not d.is_grain)


def schema(layer: Layer) -> dict[str, Any]:
    """The JSON schema of a query over this layer, with every name and value it knows as an enum."""
    dims = filterable(layer)
    values = {
        dim: {"type": "array", "items": {"type": "string", "enum": list(layer.dimensions[dim].values)}} for dim in dims
    }
    return {
        "type": "object",
        "properties": {
            "measure": {"type": "string", "enum": list(layer.measures)},
            "filters": {
                "type": "object",
                "description": "The values to keep for each dimension. An empty list keeps them all.",
                "properties": values,
                "required": list(dims),
                "additionalProperties": False,
            },
            "group_by": {"type": "array", "items": {"type": "string", "enum": list(dims)}},
            "period": {"type": "string", "description": PERIOD_FORMS},
            "compare_to": {"type": "string", "enum": [NONE, *COMPARES]},
            "grain": {"type": "string", "enum": [NONE, *GRAINS]},
            "limit": {"type": "integer", "description": "How many of the largest groups to keep, or 0 for all."},
        },
        "required": list(FIELDS),
        "additionalProperties": False,
    }


def canonical(value: object, allowed: Sequence[str], field: str) -> str:
    """The allowed name the value spells, matched without case: structured outputs don't promise an enum's case."""
    if isinstance(value, str):
        for name in allowed:
            if name.lower() == value.strip().lower():
                return name
    raise Invalid(f"{field} can't be {value!r}")


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise Invalid(f"{field} must be a list")
    return value


def parse_period(value: object) -> Period | None:
    if not isinstance(value, str):
        raise Invalid("period must be a string")
    text = value.strip()
    if not text:
        return None
    try:
        if found := RANGE.fullmatch(text):
            return Period.between(date.fromisoformat(found[1]), date.fromisoformat(found[2]))
        return Period.parse(text)
    except ValueError as exc:
        raise Invalid(f"period can't be {text!r}") from exc


def parse(data: object, layer: Layer) -> MetricQuery:
    """A query from a model's JSON, or Invalid. Every name and value must be the layer's own."""
    if not isinstance(data, Mapping) or set(data) != set(FIELDS):
        raise Invalid(f"a query has exactly the fields {', '.join(FIELDS)}")
    dims = filterable(layer)
    raw_filters = data["filters"]
    if not isinstance(raw_filters, Mapping) or not set(raw_filters) <= set(dims):
        raise Invalid("filters must map dimensions to lists of values")
    filters: dict[str, tuple[str, ...]] = {}
    for dim, values in raw_filters.items():
        allowed = layer.dimensions[dim].values
        kept = tuple(dict.fromkeys(canonical(value, allowed, dim) for value in _list(values, dim)))
        if kept:
            filters[dim] = kept
    limit = data["limit"]
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise Invalid("limit must be a whole number, 0 for all")
    compare = canonical(data["compare_to"], (NONE, *COMPARES), "compare_to")
    grain = canonical(data["grain"], (NONE, *GRAINS), "grain")
    return MetricQuery(
        measure=canonical(data["measure"], tuple(layer.measures), "measure"),
        filters=filters,
        group_by=tuple(dict.fromkeys(canonical(g, dims, "group_by") for g in _list(data["group_by"], "group_by"))),
        period=parse_period(data["period"]),
        compare_to=None if compare == NONE else cast(Compare, compare),
        grain=None if grain == NONE else cast(Grain, grain),
        limit=limit or None,
    )


def period_text(period: Period | None) -> str:
    if period is None:
        return ""
    months, start = period.months, period.start
    if months == 12 and start.month == 1:
        return str(start.year)
    if months == 3 and start.month % 3 == 1:
        return f"{start.year}-Q{start.month // 3 + 1}"
    if months == 1:
        return f"{start:%Y-%m}"
    return f"{start.isoformat()}..{period.end.isoformat()}"


def to_spec(mq: MetricQuery, layer: Layer) -> dict[str, Any]:
    return {
        "measure": mq.measure,
        "filters": {dim: list(mq.filters.get(dim, ())) for dim in filterable(layer)},
        "group_by": list(mq.group_by),
        "period": period_text(mq.period),
        "compare_to": mq.compare_to or NONE,
        "grain": mq.grain or NONE,
        "limit": mq.limit or 0,
    }
