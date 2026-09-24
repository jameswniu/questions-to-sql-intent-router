import calendar
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Literal, cast

Grain = Literal["month", "quarter", "year"]
Compare = Literal["prior_period", "prior_year"]
GRAINS: tuple[Grain, ...] = ("month", "quarter", "year")
COMPARES: tuple[Compare, ...] = ("prior_period", "prior_year")

MONTHS = tuple(calendar.month_name)[1:]


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def _months_between(start: date, end: date) -> int | None:
    """Whole months covered when the range starts on a first and ends on a month end, else None."""
    if start.day != 1 or end != month_end(end.year, end.month):
        return None
    return (end.year - start.year) * 12 + end.month - start.month + 1


def _long(day: date) -> str:
    return f"{MONTHS[day.month - 1]} {day.day}, {day.year}"


def period_label(start: date, end: date) -> str:
    months = _months_between(start, end)
    if months is None:
        return _long(start) if start == end else f"{_long(start)} to {_long(end)}"
    if months == 12 and start.month == 1:
        return str(start.year)
    if months == 3 and start.month % 3 == 1:
        return f"Q{start.month // 3 + 1} {start.year}"
    if months == 1:
        return f"{MONTHS[start.month - 1]} {start.year}"
    if start.year == end.year and start.month == 1 and end.month != 12:
        return f"{start.year} through {MONTHS[end.month - 1]}"
    if start.year == end.year:
        return f"{MONTHS[start.month - 1]} to {MONTHS[end.month - 1]} {start.year}"
    if start.month == 1 and end.month == 12:
        return f"{start.year} to {end.year}"
    return f"{MONTHS[start.month - 1]} {start.year} to {MONTHS[end.month - 1]} {end.year}"


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    label: str

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"period starts after it ends: {self.start} > {self.end}")

    @classmethod
    def between(cls, start: date, end: date) -> "Period":
        return cls(start, end, period_label(start, end))

    @classmethod
    def year(cls, year: int) -> "Period":
        return cls.between(date(year, 1, 1), date(year, 12, 31))

    @classmethod
    def quarter(cls, year: int, quarter: int) -> "Period":
        first = 3 * quarter - 2
        return cls.between(date(year, first, 1), month_end(year, first + 2))

    @classmethod
    def month(cls, year: int, month: int) -> "Period":
        return cls.between(date(year, month, 1), month_end(year, month))

    @classmethod
    def parse(cls, text: str) -> "Period":
        """Reads the compact forms the semantic layer's examples use: 2025, 2025-Q2 and 2025-01."""
        if m := re.fullmatch(r"(\d{4})", text):
            return cls.year(int(m[1]))
        if m := re.fullmatch(r"(\d{4})-Q([1-4])", text):
            return cls.quarter(int(m[1]), int(m[2]))
        if m := re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", text):
            return cls.month(int(m[1]), int(m[2]))
        raise ValueError(f"not a period: {text!r}")

    @property
    def months(self) -> int | None:
        return _months_between(self.start, self.end)

    def prior(self) -> "Period":
        """The period of the same length that ends the day before this one starts."""
        months = self.months
        if months is not None:
            return Period.between(add_months(self.start, -months), self.start - timedelta(days=1))
        days = (self.end - self.start).days + 1
        return Period.between(self.start - timedelta(days=days), self.start - timedelta(days=1))

    def years_earlier(self, years: int = 1) -> "Period":
        start = add_months(self.start, -12 * years)
        end = add_months(self.end, -12 * years)
        if self.months is not None:
            end = month_end(end.year, end.month)
        return Period.between(start, end)

    def clip(self, start: date, end: date) -> "Period | None":
        lo, hi = max(self.start, start), min(self.end, end)
        return Period.between(lo, hi) if lo <= hi else None


@dataclass(frozen=True)
class MetricQuery:
    measure: str
    filters: dict[str, tuple[str, ...]] = field(default_factory=dict)
    group_by: tuple[str, ...] = ()
    period: Period | None = None
    compare_to: Compare | None = None
    grain: Grain | None = None
    limit: int | None = None
    # Words the question used as a qualifier that the vocabulary doesn't know; resolve asks about them.
    unknown: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> "MetricQuery":
        """Builds a query from the layer's example notation, where a time grain is written as a group."""
        group_by = tuple(spec.get("group_by") or ())
        grains = [g for g in group_by if g in GRAINS]
        if len(grains) > 1:
            raise ValueError(f"more than one time grain: {grains}")
        period = spec.get("period")
        return cls(
            measure=spec["measure"],
            filters={k: tuple(v) for k, v in (spec.get("filters") or {}).items()},
            group_by=tuple(g for g in group_by if g not in GRAINS),
            period=Period.parse(str(period)) if period is not None else None,
            compare_to=cast(Compare | None, spec.get("compare_to")),
            grain=cast(Grain, grains[0]) if grains else None,
            limit=spec.get("limit"),
        )

    def with_note(self, note: str) -> "MetricQuery":
        return replace(self, notes=(*self.notes, note))


@dataclass(frozen=True)
class Clarify:
    missing: str
    question: str
    options: tuple[str, ...]
    # What was understood so far; passed back as the previous query, a reply fills in the gap.
    partial: MetricQuery | None = None


@dataclass(frozen=True)
class OutOfData:
    reason: str
    covered: Period
