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
_CLAIM = r"#\s*\d{6}\b|\bclaims?\b(?:\s+(?:number|no\.?|id))?\s*#?\s*\d{6}\b"
# The terms of the policy contract: what it covers or excludes, its deductibles, sublimits and endorsements, and
# its edition, wording or form, named in words or by number.
_WORDING = (
    r"\bcovered?\b|\bcoverage\b|\bcovers?\b|\bdeductibles?\b|\bsublimits?\b|\bexclusions?\b|\bexcluded\b|"
    r"\bendorsement\b|\bedition\b|\bwording\b|\bform\b|\bho[-\s]?(?:19|20)\d\d\b"
)
# A claim's scanned documents, which the lookup path reads: the words app.answer.scanfield.ASKS_SCAN uses.
_SCAN_DOCUMENT = r"\b(?:invoice|estimate|proof\s+of\s+loss|scan(?:ned)?|receipt|bill)\b"
# A year outside the data is a period the data can't answer, unless it names a policy edition: "HO-2023",
# "HO 2023" and "the 2023 form" (or edition, wording, policy) point at a wording, which the documents hold
# whatever years the data covers. "The 2023 policy year" is still a period.
_OUT_OF_RANGE_YEAR = (
    r"(?<![\w-])(?<!\bho\s)(?:19\d\d|20[0-1]\d|202[0-3]|202[7-9]|20[3-9]\d)\b"
    r"(?!\s+(?:form|edition|wording|policy(?!\s+year))\b)"
)

# The order is load-bearing: an id or an out-of-range period is decided before a measure is,
# and a driver question is decided before it looks like a plain measure.
_RULES: list[tuple[str, re.Pattern[str], str]] = [
    # A question naming a claim is about that claim's record, which lookup reads, unless it asks what the policy
    # wording provides for it: then the wording in force on the claim's loss date answers, and answer_qual reads
    # that date as the asker, so a claim they can't open gets lookup's own reply. "Policy" and "policyholder"
    # don't count here, since beside a claim they name its policy or its insured, and a question about one of the
    # claim's scanned documents stays with lookup, which reads them.
    ("claim_wording", re.compile(rf"^(?!.*{_SCAN_DOCUMENT})(?=.*(?:{_CLAIM}))(?=.*(?:{_WORDING}))"), "qualitative"),
    ("lookup", re.compile(_CLAIM), "lookup"),
    (
        "out_of_data",
        re.compile(
            r"\bforecast\b|\bpredict\b|\bprediction\b|\bprojection\b|\bnext\s+(?:year|quarter|month)\b|"
            rf"\bwill\s+we\b|\bexpected\s+to\b|{_OUT_OF_RANGE_YEAR}"
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
    # What the documents answer, and the data can't: the wording and its terms, a form named by its number, how
    # a thing is done or how soon (the data has no durations), what someone should or must do, and an operational
    # event such as a platform outage, which only memos record. It is decided before a measure, so "the initial
    # reserve" or "the payments platform" in such a question doesn't make it a figure.
    (
        "qualitative",
        re.compile(
            rf"\bpolicy\b|\bpolicies\b|\bpolicyholder\b|{_WORDING}|"
            r"\bhow\s+do\s+(?:we|i)\b|\bwhat'?s\s+the\s+process\b|\bprocedure\b|"
            r"\bguidelines?\b|\bprocess\s+for\b|\bhow\s+(?:long|soon|quickly)\b|\bnotice\b|\breport\s+a\s+loss\b|"
            r"\bdeadline\b|\bgrace\s+period\b|\bshould\b|\bmust\b|\bescalate\b|\boutages?\b|\bplatforms?\b"
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
