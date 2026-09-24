import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from app.semantic.query import Period, add_months, month_end

MONTH = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?"
    r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
YEAR = r"((?:19|20)\d{2})"
ORDINAL = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "last": 4}
WORD_NUMBERS = {"three": 3, "six": 6, "nine": 9, "twelve": 12, "eighteen": 18, "twenty-four": 24}


@dataclass(frozen=True)
class Found:
    period: Period
    start: int
    end: int


@dataclass(frozen=True)
class Clock:
    as_of: date
    data_end: date

    @property
    def current_quarter(self) -> Period:
        return Period.quarter(self.as_of.year, (self.as_of.month - 1) // 3 + 1)

    @property
    def current_month(self) -> Period:
        return Period.month(self.as_of.year, self.as_of.month)

    def to_date(self, start: date) -> Period:
        return Period.between(start, min(self.as_of, self.data_end))


MONTH_PREFIXES = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _month(name: str) -> int:
    return MONTH_PREFIXES.index(name[:3].lower()) + 1


def _range(first: Period, last: Period) -> Period:
    return Period.between(first.start, last.end)


def _trailing(clock: Clock, months: int) -> Period:
    last = last_full_month(clock)
    return Period.between(add_months(last.start, 1 - months), last.end)


Rule = tuple[str, Callable[[re.Match[str], Clock], Period]]

RULES: list[Rule] = [
    (
        rf"\b(?:between|from)\s+{MONTH}(?:\s+{YEAR})?\s+(?:and|to|through|until|-)\s+{MONTH}\s+{YEAR}",
        lambda m, _: _range(Period.month(int(m[2] or m[4]), _month(m[1])), Period.month(int(m[4]), _month(m[3]))),
    ),
    (
        rf"\b(?:between|from)\s+{YEAR}\s+(?:and|to|through|until|-)\s+{YEAR}",
        lambda m, _: _range(Period.year(int(m[1])), Period.year(int(m[2]))),
    ),
    (rf"\bsince\s+{MONTH}\s+{YEAR}", lambda m, c: c.to_date(date(int(m[2]), _month(m[1]), 1))),
    (rf"\bsince\s+{YEAR}", lambda m, c: c.to_date(date(int(m[1]), 1, 1))),
    (rf"\bq([1-4])\s*(?:of\s+)?{YEAR}\b", lambda m, _: Period.quarter(int(m[2]), int(m[1]))),
    (rf"\b{YEAR}\s*-?\s*q([1-4])\b", lambda m, _: Period.quarter(int(m[1]), int(m[2]))),
    (
        rf"\b(first|1st|second|2nd|third|3rd|fourth|4th|last)\s+quarter\s+(?:of\s+)?{YEAR}",
        lambda m, _: Period.quarter(int(m[2]), ORDINAL[m[1].lower()]),
    ),
    (rf"\b{MONTH},?\s+(?:of\s+)?{YEAR}\b", lambda m, _: Period.month(int(m[2]), _month(m[1]))),
    (rf"\b{YEAR}-(0[1-9]|1[0-2])\b", lambda m, _: Period.month(int(m[1]), int(m[2]))),
    (
        r"\b(?:year[\s-]to[\s-]date|ytd|this\s+year(?:\s+so\s+far)?|so\s+far\s+this\s+year)\b",
        lambda _, c: c.to_date(date(c.as_of.year, 1, 1)),
    ),
    (r"\blast\s+year\b", lambda _, c: Period.year(c.as_of.year - 1)),
    (r"\bnext\s+year\b", lambda _, c: Period.year(c.as_of.year + 1)),
    (r"\b(?:last|previous|prior)\s+quarter\b", lambda _, c: c.current_quarter.prior()),
    (r"\bthis\s+quarter\b", lambda _, c: c.current_quarter),
    (r"\bnext\s+quarter\b", lambda _, c: Period.quarter(*_next_quarter(c))),
    (r"\blast\s+month\b", lambda _, c: c.current_month.prior()),
    (r"\bthis\s+month\b", lambda _, c: c.current_month),
    (r"\bnext\s+month\b", lambda _, c: Period.month(*_year_month(add_months(c.current_month.start, 1)))),
    (
        r"\b(?:last|past|trailing|previous)\s+(\d{1,2}|three|six|nine|twelve|eighteen|twenty-four)\s+months\b",
        lambda m, c: _trailing(c, int(m[1]) if m[1].isdigit() else WORD_NUMBERS[m[1].lower()]),
    ),
    (rf"\b{YEAR}\b", lambda m, _: Period.year(int(m[1]))),
]
COMPILED = [(re.compile(pattern, re.IGNORECASE), build) for pattern, build in RULES]


def _year_month(day: date) -> tuple[int, int]:
    return day.year, day.month


def _next_quarter(clock: Clock) -> tuple[int, int]:
    start = add_months(clock.current_quarter.start, 3)
    return start.year, (start.month - 1) // 3 + 1


def find_periods(text: str, clock: Clock) -> list[Found]:
    """Every period the text names, in the order they appear. Earlier rules claim their words first."""
    taken = [False] * len(text)
    found: list[Found] = []
    for pattern, build in COMPILED:
        for m in pattern.finditer(text):
            if any(taken[m.start() : m.end()]):
                continue
            try:
                period = build(m, clock)
            except ValueError:
                continue
            taken[m.start() : m.end()] = [True] * (m.end() - m.start())
            found.append(Found(period, m.start(), m.end()))
    return sorted(found, key=lambda f: f.start)


def last_full_month(clock: Clock) -> Period:
    end = clock.data_end
    if end != month_end(end.year, end.month):
        end = add_months(end, -1)
    return Period.month(end.year, end.month)
