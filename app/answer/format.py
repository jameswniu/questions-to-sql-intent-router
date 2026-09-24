from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

Format = Literal["integer", "currency", "percent"]
Number = Decimal | int | float


@dataclass(frozen=True)
class NumberRef:
    """Ties a figure in the answer text to the result cell, or the arithmetic on cells, it came from."""

    value: Decimal
    display: str
    column: str
    row_index: int | None
    derivation: str


def as_decimal(value: Number) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _whole(value: Decimal) -> str:
    return f"{value.quantize(Decimal(1), rounding=ROUND_HALF_UP):,}"


def format_value(value: Number, fmt: Format) -> str:
    number = as_decimal(value)
    sign = "-" if number < 0 else ""
    if fmt == "currency":
        return f"{sign}${_whole(abs(number))}"
    if fmt == "percent":
        return f"{(number * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"
    return _whole(number)


def format_change(delta: Number, fmt: Format) -> str:
    """The size of a change, without its sign. A change in a rate is in percentage points."""
    size = abs(as_decimal(delta))
    if fmt == "percent":
        points = (size * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        return f"{points} point" if points == 1 else f"{points} points"
    return format_value(size, fmt)


def format_ratio(value: Number) -> str:
    return format_value(abs(as_decimal(value)), "percent")


def long_date(day: date) -> str:
    return f"{day:%B} {day.day}, {day.year}"


def ref(value: Number, fmt: Format, column: str, row_index: int | None, derivation: str) -> NumberRef:
    return NumberRef(as_decimal(value), format_value(value, fmt), column, row_index, derivation)


def change_refs(
    current: Number, prior: Number, fmt: Format, rows: tuple[int, int], source: str = "value"
) -> tuple[NumberRef, ...]:
    """The change and, when the prior value isn't zero, the percent change, each with how it was computed.

    source is how one row's figure is read, such as "value" or "num / den"; it is applied to both rows.
    """
    now, then = as_decimal(current), as_decimal(prior)
    delta = now - then
    first, second = (f"({source})[{row}]" if " " in source else f"{source}[{row}]" for row in rows)
    where = f"{first} - {second}"
    refs = [NumberRef(delta, format_change(delta, fmt), source, None, where)]
    if then != 0:
        pct = delta / abs(then)
        refs.append(NumberRef(pct, format_ratio(pct), source, None, f"({where}) / {second}"))
    return tuple(refs)
