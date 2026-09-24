from collections.abc import Iterable, Mapping
from datetime import date
from functools import cache
from typing import Any

from app.config import policy
from app.semantic.layer import Layer, Measure
from app.semantic.query import MONTHS, MetricQuery

QUALIFYING = ("status", "peril", "channel")


@cache
def state_names() -> dict[str, str]:
    return {code: name for states in policy()["regions"].values() for code, name in states.items()}


@cache
def state_regions() -> dict[str, str]:
    return {code: region for region, states in policy()["regions"].items() for code in states}


def join(words: Iterable[str], last: str = "and") -> str:
    items = list(words)
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} {last} {items[-1]}"


def display_name(measure: Measure) -> str:
    return next((s for s in measure.synonyms if " " in s and not s.startswith(("how ", "number "))), measure.label)


def is_plural(measure: Measure) -> bool:
    name = display_name(measure)
    return name.startswith("claims ") or name.endswith(("losses", "reserves", "payments"))


def places(filters: Mapping[str, tuple[str, ...]]) -> str:
    states = [state_names().get(code, code) for code in filters.get("state", ())]
    return join(states or [f"the {region}" for region in filters.get("region", ())])


def place(filters: Mapping[str, tuple[str, ...]]) -> str:
    names = places(filters)
    return f"in {names}" if names else ""


def qualifier(filters: Mapping[str, tuple[str, ...]]) -> str:
    words = [join(filters[dim], "or") for dim in QUALIFYING if filters.get(dim)]
    return f"{' '.join(words)} claims" if words else ""


def subject(mq: MetricQuery, layer: Layer, *, capital: bool = True) -> str:
    measure = layer.measures[mq.measure]
    name = display_name(measure)
    parts = [name[0].upper() + name[1:] if capital else name]
    if qual := qualifier(mq.filters):
        parts.append(f"for {qual}")
    if where := place(mq.filters):
        parts.append(where)
    return " ".join(parts)


def group_label(dim: str, value: Any) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, date):
        if dim == "month":
            return f"{MONTHS[value.month - 1][:3]} {value.year}"
        if dim == "quarter":
            return f"Q{(value.month - 1) // 3 + 1} {value.year}"
        if dim == "year":
            return str(value.year)
    if dim == "state":
        return state_names().get(str(value), str(value))
    if dim == "month" and isinstance(value, str) and len(value) == 7:
        return f"{MONTHS[int(value[5:]) - 1][:3]} {value[:4]}"
    if dim == "quarter" and isinstance(value, str) and "-Q" in value:
        return f"{value[5:]} {value[:4]}"
    return str(value)


def row_label(keys: Iterable[str], values: Iterable[Any]) -> str:
    return ", ".join(group_label(dim, value) for dim, value in zip(keys, values, strict=True))
