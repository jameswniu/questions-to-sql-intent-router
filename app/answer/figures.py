import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Form = Literal["cents", "dollars", "scaled", "percent", "decimal", "integer"]


@dataclass(frozen=True)
class Written:
    """A figure as it appears in answer text. value is its size in the unit written, with any scale word applied."""

    token: str
    value: Decimal
    form: Form
    places: int
    scale: Decimal = Decimal(1)
    negative: bool = False


MONTH = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?"
)
# Digits that label something rather than measure it. They are blanked before figures are read.
NOT_QUANTITIES = re.compile(
    r"""
    \[[^\]]*\]                                        # citation markers
    | \S*\#\S*                                        # chunk ids, #100013
    | \b\d{4}-\d{2}(?:-\d{2})?\b                      # 2025-04, 2025-04-01
    | \b\d{1,2}/\d{1,2}/\d{2,4}\b                     # 4/1/2025
    | \b\d{1,2}:\d{2}\b                               # 9:30
    | \bMONTH\s+\d{1,2}(?:st|nd|rd|th)?\b(?!,\d)      # June 30
    | \b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?MONTH     # 30 June
    | \b\d+(?:st|nd|rd|th)\b                          # ordinals
    | \b[A-Za-z]+-?\d[\w-]*                           # the Q2 of Q2 2025, H1, FY2025, HO-2025
    | (?i:\b(?:claims?|polic(?:y|ies))\s+(?:no\.?\s*|numbers?\s*)?\d+(?:(?:,\s*|,?\s+(?:and|or)\s+)\d+)*)
    | (?:\b(?i:section|sec\.|clause|article|page)|§)\s*\d+(?:\.\d+)*
    """.replace("MONTH", MONTH),
    re.VERBOSE,
)
FIGURE = re.compile(
    r"""
    (?<![\w.])
    (?P<sign>[-−](?=\$?\d))?
    (?P<dollar>\$)?
    (?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)
    (?:\s?(?P<word>thousand|million|billion)s?\b|(?P<letter>[KkMmBb])(?![A-Za-z]))?
    (?:
        \s?(?P<percent>%|percent\b|per\scent\b)
      | [\s-](?P<points>(?:percentage\s)?points?\b|pts?\b|pp\b)
      | \s(?P<dollars>dollars?\b)
    )?
    (?![\w%])
    """,
    re.VERBOSE,
)
SCALES = {"thousand": Decimal(1_000), "million": Decimal(1_000_000), "billion": Decimal(1_000_000_000)}
SCALE_LETTERS = {"k": "thousand", "m": "million", "b": "billion"}


def figures(text: str) -> tuple[Written, ...]:
    """Every quantity written in the text, leaving out years, quarters, dates, claim numbers and form names."""
    blanked = NOT_QUANTITIES.sub(lambda m: " " * len(m.group()), text)
    found = []
    for match in FIGURE.finditer(blanked):
        number = match["number"]
        digits = number.replace(",", "")
        places = len(digits.partition(".")[2])
        scale_word = match["word"] or SCALE_LETTERS.get((match["letter"] or "").lower())
        scale = SCALES[scale_word] if scale_word else Decimal(1)
        form: Form
        if match["percent"] or match["points"]:
            form = "percent"
        elif scale_word:
            form = "scaled"
        elif match["dollar"] or match["dollars"]:
            form = "cents" if places else "dollars"
        elif places:
            form = "decimal"
        elif "," not in number and (len(digits) >= 5 or (len(digits) == 4 and 1900 <= int(digits) <= 2099)):
            continue  # a year, or a claim or policy number: quantities are written with thousands separators
        else:
            form = "integer"
        token = text[match.start() : match.end()].strip()
        found.append(Written(token, Decimal(digits) * scale, form, places, scale, bool(match["sign"])))
    return tuple(found)
