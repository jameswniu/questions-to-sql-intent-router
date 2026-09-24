import re
from dataclasses import dataclass, field

from app.semantic.query import Compare, Grain

YEAR_OVER_YEAR = re.compile(
    r"\b(?:year[\s-]over[\s-]year|yoy|the\s+year\s+before|(?:a|one)\s+year\s+(?:earlier|before|ago)"
    r"|same\s+(?:period|quarter|month|time)\s+(?:last\s+year|a\s+year\s+(?:earlier|before|ago)))\b",
    re.IGNORECASE,
)
PERIOD_OVER_PERIOD = re.compile(
    r"\b(?:quarter[\s-]over[\s-]quarter|month[\s-]over[\s-]month|qoq|the\s+(?:quarter|month|period)\s+before"
    r"|(?:the\s+)?(?:prior|previous|preceding)\s+(?:period|quarter|month))\b",
    re.IGNORECASE,
)
PRIOR_YEAR = re.compile(r"\b(?:the\s+)?(?:prior|previous|preceding)\s+year\b", re.IGNORECASE)
VERSUS = re.compile(r"\b(?:versus|vs\.?|compared\s+(?:to|with)|against|relative\s+to)(?=\s|$)", re.IGNORECASE)
DIMENSION_WORDS = r"(month|quarter|year|region|state|peril|channel|status|cause(?:\s+of\s+loss)?|loss\s+type)"
GROUP = re.compile(
    rf"\b(?:by|per|for\s+(?:each|every)|each|across(?:\s+all)?|(?:broken\s+down|split|grouped)\s+by)\s+"
    rf"(?:the\s+)?{DIMENSION_WORDS}(?:e?s)?\b",
    re.IGNORECASE,
)
GROUP_MORE = re.compile(
    rf"\s*(?:,\s*(?:and\s+)?|\s+and\s+|\s*&\s*)(?:by\s+)?{DIMENSION_WORDS}(?:e?s)?\b", re.IGNORECASE
)
TOP = re.compile(rf"\btop\s+(\d{{1,2}})\s+{DIMENSION_WORDS}(?:e?s)?\b", re.IGNORECASE)
WHICH = re.compile(
    rf"\b(?:which|what)\s+{DIMENSION_WORDS}\b.*\b(?:most|highest|largest|biggest|worst)\b", re.IGNORECASE
)
ADVERBS: dict[str, Grain] = {"monthly": "month", "quarterly": "quarter", "yearly": "year", "annually": "year"}
ALIASES = {"cause": "peril", "cause of loss": "peril", "loss type": "peril"}
QUALIFIER = re.compile(r"\b([a-z][a-z'-]{2,})\s+(?:claims?|damage|losses)\b", re.IGNORECASE)
QUALIFIER_STOPWORDS = (
    "all any the our my your their these those this that many much total new open closed denied reported "
    "paid insurance property home homeowner homeowners policy such other more fewer less most some few "
    "recent prior current previous past last next did were was have had has with and for how what which "
    "number count average individual outstanding pending active existing filed same big large small "
    "largest biggest top approved settled submitted made get got see show give list are there where when "
    "into from about over under per each every its it's whose approx"
)
NOT_QUALIFIERS = frozenset(QUALIFIER_STOPWORDS.split())
# A capitalised name after a preposition reads as a place or a proper noun the vocabulary should know.
PLACE = re.compile(r"\b(?:in|for|from|at)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b")
MONTH_PREFIXES = frozenset({"jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"})
NOT_PLACES = frozenset({"the", "our", "all", "each", "every", "total", "what", "which"})
NEGATION = re.compile(
    r"\b(?:ignor(?:e|ing)|exclud(?:e|ing)|except|without|not|other\s+than|apart\s+from|leav(?:e|ing)\s+out"
    r"|but\s+not)\s+(?:the\s+|any\s+|all\s+)?$",
    re.IGNORECASE,
)


@dataclass
class Scan:
    text: str
    taken: list[bool] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.taken = [False] * len(self.text)

    def claim(self, start: int, end: int) -> None:
        self.taken[start:end] = [True] * (end - start)

    def free(self, start: int, end: int) -> bool:
        return not any(self.taken[start:end])


def _dimension_name(word: str) -> str:
    word = re.sub(r"\s+", " ", word.lower())
    return ALIASES.get(word, word)


GRAIN_WORDS: dict[str, Grain] = {"month": "month", "quarter": "quarter", "year": "year"}


def grouping(scan: Scan) -> tuple[list[str], Grain | None, int | None]:
    found: list[tuple[re.Match[str], str, int | None]] = []
    for m in GROUP.finditer(scan.text):
        found.append((m, m[1], None))
        end = m.end()
        while more := GROUP_MORE.match(scan.text, end):
            found.append((more, more[1], None))
            end = more.end()
    found += [(m, m[2], int(m[1])) for m in TOP.finditer(scan.text)]
    found += [(m, m[1], 1) for m in WHICH.finditer(scan.text)]
    group_by: list[str] = []
    grain: Grain | None = None
    limit: int | None = None
    for m, word, top in found:
        name = _dimension_name(word)
        if name in GRAIN_WORDS:
            grain = grain or GRAIN_WORDS[name]
        elif name not in group_by:
            group_by.append(name)
        limit = top or limit
        scan.claim(m.start(), m.end())
    for word, value in ADVERBS.items():
        if re.search(rf"\b{word}\b", scan.text, re.IGNORECASE):
            grain = grain or value
    return group_by, grain, limit


COMPARE_CUES: tuple[tuple[re.Pattern[str], Compare], ...] = (
    (YEAR_OVER_YEAR, "prior_year"),
    (PERIOD_OVER_PERIOD, "prior_period"),
)


def compare_cues(scan: Scan) -> tuple[Compare | None, bool]:
    compare: Compare | None = None
    versus = False
    for pattern, kind in COMPARE_CUES:
        for m in pattern.finditer(scan.text):
            compare = compare or kind
            scan.claim(m.start(), m.end())
    for m in VERSUS.finditer(scan.text):
        versus = True
        scan.claim(m.start(), m.end())
    if versus and compare is None and (prior := PRIOR_YEAR.search(scan.text)):
        compare = "prior_year"
        scan.claim(prior.start(), prior.end())
    return compare, versus


def unknown_qualifiers(scan: Scan) -> tuple[str, ...]:
    """Words that qualify the question but that nothing recognised, such as "hurricane claims" or "in Nevada"."""
    words = []
    for m in QUALIFIER.finditer(scan.text):
        word = m[1].lower()
        if scan.free(m.start(1), m.end(1)) and word not in NOT_QUALIFIERS and not word.endswith("ly"):
            words.append(word)
    for m in PLACE.finditer(scan.text):
        first = m[1].split()[0].lower()
        if scan.free(m.start(1), m.end(1)) and first[:3] not in MONTH_PREFIXES and first not in NOT_PLACES:
            words.append(m[1])
    return tuple(dict.fromkeys(words))
