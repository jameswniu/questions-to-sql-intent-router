import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

TOKENS = {"ssn": "[SSN]", "dob": "[DOB]", "phone": "[PHONE]", "email": "[EMAIL]", "name": "[NAME]"}

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SSN_SHAPED = re.compile(r"(?<![\d-])\d{3}([- ])\d{2}\1\d{4}(?![\d-])")
# Nine bare digits are an SSN only when the text says so just before them.
SSN_BARE = re.compile(
    r"(?i:\b(?:ssn|ss#|social security(?: number)?|social)\W{0,3}(?:no\.?|number|is|#)?\W{0,3})(\d{9})\b"
)
PHONE = re.compile(r"(?<![\d$.,/-])(?:\(\d{3}\)\s?\d{3}[-.\s]\d{4}|\d{3}([-.\s])\d{3}\1\d{4}|\d{10})(?![\d/-])")
# A date is a date of birth only in context. Dates of loss, report and payment dates are left alone.
DOB = re.compile(
    r"(?i:\b(?:dob|d\.o\.b\.?|date of birth|birth ?date|born(?: on)?)\W{0,3}(?:is\W{1,2}|was\W{1,2})?)"
    r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Z][a-z]{2,8}\.? \d{1,2},? \d{4})"
)
TITLES = r"(?:(?:Mr|Mrs|Ms|Dr)\.?\s+)?"
# First names that are also everyday words are masked only next to the surname.
COMMON_WORDS = frozenset(
    {
        "April", "Art", "Bill", "Chase", "Dawn", "Don", "Faith", "Frank", "Gene", "Grace", "Grant", "Guy", "Holly",
        "Hope", "Hunter", "Ivy", "Jack", "Joy", "June", "Mark", "May", "Pat", "Price", "Ray", "Rich", "Rob", "Rose",
        "Sky", "Summer", "Will",
    }
)  # fmt: skip


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    kind: str


@dataclass(frozen=True)
class Masked:
    text: str
    spans: tuple[Span, ...]

    @property
    def counts(self) -> Counter[str]:
        return Counter(span.kind for span in self.spans)


def _name_patterns(names: Sequence[str]) -> list[re.Pattern[str]]:
    patterns = []
    for full in names:
        parts = full.split()
        first, last = parts[0], parts[-1]
        patterns.append(re.compile(rf"\b{TITLES}{re.escape(first)}\s+{re.escape(last)}\b"))
        patterns.append(re.compile(rf"\b{TITLES}{re.escape(last)}\b"))
        if len(first) > 2 and first not in COMMON_WORDS:
            patterns.append(re.compile(rf"\b{re.escape(first)}\b"))
    return patterns


def find(text: str, names: Sequence[str] = ()) -> list[Span]:
    """Every PII span, earliest kind first: an email wins over the name inside it, an SSN over a phone."""
    candidates: list[Span] = []
    candidates += [Span(m.start(), m.end(), "email") for m in EMAIL.finditer(text)]
    candidates += [Span(m.start(), m.end(), "ssn") for m in SSN_SHAPED.finditer(text)]
    candidates += [Span(m.start(1), m.end(1), "ssn") for m in SSN_BARE.finditer(text)]
    candidates += [Span(m.start(1), m.end(1), "dob") for m in DOB.finditer(text)]
    candidates += [Span(m.start(), m.end(), "phone") for m in PHONE.finditer(text)]
    for pattern in _name_patterns(names):
        candidates += [Span(m.start(), m.end(), "name") for m in pattern.finditer(text)]
    taken: list[Span] = []
    for span in candidates:
        if all(span.end <= t.start or span.start >= t.end for t in taken):
            taken.append(span)
    return sorted(taken, key=lambda s: s.start)


def mask(text: str, names: Sequence[str] = ()) -> Masked:
    spans = find(text, names)
    out, cursor = [], 0
    for span in spans:
        out += [text[cursor : span.start], TOKENS[span.kind]]
        cursor = span.end
    out.append(text[cursor:])
    return Masked("".join(out), tuple(spans))
