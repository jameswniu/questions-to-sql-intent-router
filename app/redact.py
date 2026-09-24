import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_SSN = re.compile(r"(?<!\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d)")
# Policyholder phone numbers are in the data too, and no group of one is long enough for the digit rule.
_PHONE = re.compile(r"(?<!\d)(?:\(\d{3}\)\s?|\d{3}[-. ])\d{3}[-. ]\d{4}(?!\d)")
_LONG_NUMBER = re.compile(r"\d{5,}")


def redact(text: str) -> str:
    """Masks what could point at a person or a claim and keeps the wording, so logged questions stay readable."""
    for pattern, mask in ((_EMAIL, "[email]"), (_SSN, "[ssn]"), (_PHONE, "[phone]"), (_LONG_NUMBER, "[number]")):
        text = pattern.sub(mask, text)
    return text


def sql_for_span(statement: str) -> str | None:
    """The statement with every literal turned into a placeholder, or None when it does not parse."""
    try:
        tree = sqlglot.parse_one(statement, read="postgres")
    except SqlglotError:
        return None
    return tree.transform(lambda node: exp.Placeholder() if isinstance(node, exp.Literal) else node).sql("postgres")
