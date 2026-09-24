import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Turn:
    route: str
    question: str


@dataclass(frozen=True)
class Routed:
    route: str
    rule: str
    source: str


_MEASURE = (
    r"(?:paid\s+losses?|paid\s+out|paid|payouts?|payments?|losses?|loss\s+ratio|claim\s+count|"
    r"claims?\s+paid|claims?\s+reported|claims?\s+filed|claims?|number\s+of\s+claims|denial\s+rate|"
    r"denials?|denied|severity|average\s+(?:severity|claim|payout|paid|cost)|reserves?|premiums?|"
    r"how\s+many|count\s+of)"
)
_PERIOD = (
    r"(?:\b20(?:24|25|26)\b|\bq[1-4]\b|\bh[12]\b|\bquarter(?:ly)?\b|\bmonth(?:ly)?\b|\byear\b|"
    r"\blast\s+(?:year|month|quarter)\b|\bthis\s+(?:year|month|quarter)\b|\byear\s+to\s+date\b|\bytd\b|"
    r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b)"
)
_DIM = (
    r"(?:\b(?:north|south|east|west|northern|southern|eastern|western)\b|"
    r"\b(?:mn|wi|tx|ok|pa|oh|co|az)\b|"
    r"\b(?:minnesota|wisconsin|texas|oklahoma|pennsylvania|ohio|colorado|arizona)\b|"
    r"\b(?:water|wind|hail|fire|theft|mold|windstorm|hailstorm|burglary)\b|"
    r"\b(?:phone|app|agent|online|website|web|mobile|broker)\b|"
    r"\bby\s+(?:region|state|peril|channel|month|quarter|year)\b)"
)

# The order is load-bearing: an id or an out-of-range period is decided before a measure is,
# and a driver question is decided before it looks like a plain measure.
_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("lookup", re.compile(r"#\s*\d{4,6}\b|\bclaims?\b(?:\s+(?:number|no\.?|id))?\s*#?\s*\d{4,6}\b"), "lookup"),
    (
        "out_of_data",
        re.compile(
            r"\bforecast\b|\bpredict\b|\bprediction\b|\bprojection\b|\bnext\s+(?:year|quarter|month)\b|"
            r"\bwill\s+we\b|\bexpected\s+to\b|(?<![\w-])(?:19\d\d|20[0-1]\d|202[0-3]|202[7-9]|20[3-9]\d)\b"
        ),
        "out_of_data",
    ),
    (
        "why",
        re.compile(
            r"\b(?:why|what\s+(?:caused|drove|explains|explain))\b.*\b(?:jump(?:ed)?|spike[d]?|surge[d]?|"
            r"rise|rose|risen|increase[d]?|grew|growth|drop(?:ped)?|fall|fell|decline[d]?|higher|lower|"
            r"so\s+high|so\s+low|high|low|change[d]?|up|down|paid|losses?|loss|claims?|denials?|"
            r"denial\s+rate|severity|reserves?|premium|payments?|loss\s+ratio)\b"
        ),
        "why",
    ),
    (
        "qualitative",
        re.compile(
            r"\bpolicy\b|\bpolicies\b|\bpolicyholder\b|\bcovered?\b|\bcoverage\b|\bcovers?\b|"
            r"\bdeductibles?\b|\bsublimits?\b|\bexclusions?\b|\bexcluded\b|\bendorsement\b|\bedition\b|"
            r"\bwording\b|\bform\b|\bhow\s+do\s+(?:we|i)\b|\bwhat'?s\s+the\s+process\b|\bprocedure\b|"
            r"\bguidelines?\b|\bprocess\s+for\b|\bhow\s+long\b|\bnotice\b|\breport\s+a\s+loss\b|"
            r"\bdeadline\b|\bgrace\s+period\b|\bwhen\s+should\b|\bescalate\b"
        ),
        "qualitative",
    ),
    ("quant_reserve", re.compile(r"\b(?:open\s+reserves?|outstanding\s+reserves?|reserves?)\b"), "quantitative"),
    ("quant_measure", re.compile(rf"(?=.*{_MEASURE})(?=.*(?:{_PERIOD}|{_DIM}))"), "quantitative"),
    (
        "clarify",
        re.compile(r"\bhow\s+much\b|\bhow\s+many\b|\bwhat'?s\s+the\s+(?:trend|total|average|number|count)\b|\btrend\b"),
        "clarify",
    ),
]

_FOLLOWUP_CUE = re.compile(r"^(?:and|also|what\s+about|how\s+about|by|in|for)\b")
_REFINEMENT = re.compile(rf"{_PERIOD}|{_DIM}")


def route(question: str, previous: Turn | None = None) -> Routed:
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", question).lower()).strip()
    for name, pattern, target in _RULES:
        if pattern.search(text):
            return Routed(route=target, rule=name, source="rule")
    if previous is not None and (_FOLLOWUP_CUE.search(text) or _REFINEMENT.search(text)):
        return Routed(route=previous.route, rule="followup", source="rule")
    return Routed(route="residue", rule="residue", source="rule")
