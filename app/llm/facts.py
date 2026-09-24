import json
import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

# One word with no spaces, and then only a handle this process minted, a value from a fixed vocabulary, a period
# or a day, or one of the statuses below. A sentence, an instruction, a title or a chunk id's heading slug is none
# of these.
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,39}")
HANDLE = re.compile(r"[a-z]{1,2}[0-9]{1,3}")
PERIOD = re.compile(r"\d{4}(?:-(?:Q[1-4]|\d{2}(?:-\d{2})?))?")
KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")
STATUSES = frozenset(
    {"ok", "clarify", "out_of_data", "not_allowed", "unsafe", "not_supported", "none_found", "none_valid"}
)


class IsolationViolation(Exception):
    """A request that would break the isolation rule. It is raised while the request is built, so it is never sent."""


@dataclass(frozen=True)
class Token:
    """A string a tool result may carry. Without a vocabulary it has to be a handle, a period or a status."""

    value: str
    vocabulary: frozenset[str] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not TOKEN.fullmatch(self.value):
            raise IsolationViolation(f"{self.value!r} is not a single token")
        if self.vocabulary is not None:
            known = self.value in self.vocabulary
        else:
            known = bool(HANDLE.fullmatch(self.value) or PERIOD.fullmatch(self.value)) or self.value in STATUSES
        if not known:
            raise IsolationViolation(f"{self.value!r} is not a handle, a period, a status or a known value")

    @classmethod
    def handle(cls, value: str) -> "Token":
        if not HANDLE.fullmatch(value):
            raise IsolationViolation(f"{value!r} is not a handle this process minted")
        return cls(value)

    @classmethod
    def word(cls, value: str, vocabulary: Collection[str]) -> "Token":
        return cls(value, frozenset(vocabulary))

    @classmethod
    def day(cls, value: date) -> "Token":
        return cls(value.isoformat())


# What a tool result is built from: ids, enums and numbers, in lists and objects keyed by field names.
type Fact = None | bool | int | float | Decimal | Token | list[Fact] | tuple[Fact, ...] | Mapping[str, Fact]


def check(value: object, where: str = "result") -> None:
    """Raises unless the value holds nothing but tokens, numbers, booleans and nulls. A plain string never passes."""
    if value is None or isinstance(value, bool | int | Token):
        return
    if isinstance(value, float | Decimal):
        if not (value.is_finite() if isinstance(value, Decimal) else math.isfinite(value)):
            raise IsolationViolation(f"{where} holds a number that isn't finite")
        return
    if isinstance(value, Mapping):
        for key, inner in value.items():
            if not isinstance(key, str) or not KEY.fullmatch(key):
                raise IsolationViolation(f"{where} has a key {key!r} that isn't a field name")
            check(inner, f"{where}.{key}")
        return
    if isinstance(value, list | tuple):
        for index, inner in enumerate(value):
            check(inner, f"{where}[{index}]")
        return
    raise IsolationViolation(f"{where} holds a {type(value).__name__}; a tool result carries ids, enums and numbers")


def _plain(value: object) -> object:
    if isinstance(value, Token):
        return value.value
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"{type(value).__name__} can't be written into a tool result")


def to_json(value: Fact) -> str:
    check(value)
    return json.dumps(value, default=_plain, separators=(",", ":"))
