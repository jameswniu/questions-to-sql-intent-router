import re
from datetime import date

from app.semantic.timephrase import MONTH, MONTH_PREFIXES

CLAIM_ID = re.compile(r"(?:\bclaim\s*(?:number|no\.?)?\s*#?\s*|#\s*)(\d{6})\b", re.IGNORECASE)
LONG_DATE = re.compile(rf"\b{MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+((?:19|20)\d{{2}})\b", re.IGNORECASE)
ISO_DATE = re.compile(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b")
US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})\b")
EDITION = re.compile(
    r"\bHO[-\s]?((?:19|20)\d{2})\b|\b((?:19|20)\d{2})\s+(?:form|edition|wording|policy)\b", re.IGNORECASE
)
ISSUED = re.compile(rf"\b(?:issued|date:)\s+{LONG_DATE.pattern}", re.IGNORECASE)


def claim_id_in(text: str) -> int | None:
    found = CLAIM_ID.search(text)
    return int(found.group(1)) if found else None


def _day(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _long(match: re.Match[str]) -> date | None:
    month, day, year = match.group(1, 2, 3)
    return _day(int(year), MONTH_PREFIXES.index(month[:3].lower()) + 1, int(day))


def date_in(text: str) -> date | None:
    """A calendar day written out in the text: June 1, 2024, 2024-06-01 or 6/1/2024."""
    if found := LONG_DATE.search(text):
        return _long(found)
    if found := ISO_DATE.search(text):
        return _day(int(found.group(1)), int(found.group(2)), int(found.group(3)))
    if found := US_DATE.search(text):
        return _day(int(found.group(3)), int(found.group(1)), int(found.group(2)))
    return None


def edition_in(text: str) -> str | None:
    """The policy edition a question names, as HO-2023 for 'HO-2023', 'the 2023 form' or 'the 2023 edition'."""
    found = EDITION.search(text)
    return f"HO-{found.group(1) or found.group(2)}" if found else None


def issued_on(text: str) -> date | None:
    """The date a memo or bulletin gives for itself, from its 'Issued May 9, 2025' or 'Date: October 15, 2025' line."""
    found = ISSUED.search(text)
    return _long(found) if found else None
