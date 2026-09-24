import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from typing import Any

import yaml

from app.config import ROOT

MAX_LENGTH = 1000

Reason = str


@dataclass(frozen=True)
class Refusal:
    reason: Reason
    message: str


_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"))


def _normalize(question: str) -> str:
    folded = unicodedata.normalize("NFKC", question).lower().translate(_ZERO_WIDTH)
    return re.sub(r"\s+", " ", folded).strip()


_INJECTION = re.compile(
    r"""
    \b(ignore|disregard|forget|override)\b[^.]{0,40}\b(previous|prior|earlier|above|all)\b[^.]{0,25}
        \b(instruction|instructions|rule|rules|prompt|prompts|context|direction|directions)\b
    | \bsystem\s+prompt\b
    | \bdeveloper\s+mode\b
    | \bjailbreak\b
    | \byou\s+are\s+now\b
    | \b(act\s+as|pretend\s+(to\s+be|you\s+are|you're))\b[^.]{0,30}
        \b(supervisor|admin|administrator|root|another|superuser)\b
    | \b(reveal|print|show|repeat|display|expose|output)\b[^.]{0,30}\b(your|the)\b[^.]{0,15}
        \b(instructions?|prompt|rules|system|configuration)\b
    | ^\s*system\s*:
    | \b(show|list|give|dump|reveal)\b[^.]{0,40}\bssns?\b
    | \bssns?\b[^.]{0,20}\b(of|for)\b
    """,
    re.VERBOSE,
)

_CODING = re.compile(
    r"""
    \b(write|create|generate|compose|produce|code\s+up|give\s+me|show\s+me)\b[^.]{0,40}
        \b(code|script|scripts|function|functions|regex|program|snippet|query\s+for\s+me)\b
    | \bleetcode\b
    | \bpalindrome\b
    | \bfizzbuzz\b
    | \breverse\s+(a|the)\s+string\b
    """,
    re.VERBOSE,
)

_CHIT_CHAT = re.compile(
    r"""
    ^(hi|hii|hey|hello|yo|sup|greetings|good\s+(morning|afternoon|evening|night))\b
    | \bhi\s+there\b
    | \bhow\s+are\s+you\b
    | \btell\s+me\s+a\s+joke\b
    | ^(thanks|thank\s+you|thx|cheers)\b
    | \bwhat'?s\s+up\b
    | ^who\s+are\s+you\b
    """,
    re.VERBOSE,
)

_OFF_TOPIC_CUES = re.compile(
    r"\b(car|engine|recipe|cook|cooking|weather|sports?|super\s*bowl|football|basketball|"
    r"stocks?|movie|film|song|music|travel|flight|vacation|restaurant|homework)\b"
)


@cache
def _domain_vocabulary() -> frozenset[str]:
    with (ROOT / "semantic" / "claims.yaml").open() as fh:
        doc: dict[str, Any] = yaml.safe_load(fh)
    words: set[str] = set()

    def harvest(value: Any) -> None:
        if isinstance(value, str):
            words.update(re.findall(r"[a-z]{2,}", value.lower()))
        elif isinstance(value, list):
            for item in value:
                harvest(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                harvest(key)
                harvest(item)

    for measure in doc.get("measures", {}).values():
        harvest(measure.get("label"))
        harvest(measure.get("synonyms"))
    harvest(doc.get("dimensions"))
    harvest(_EXTRA_TERMS)
    words -= _STOPWORDS
    return frozenset(words)


_EXTRA_TERMS = """
    claim claims policy policies policyholder policyholders peril perils deductible deductibles
    adjuster adjusters payment payments loss losses premium premiums coverage covered cover severity
    denial denials denied deny reserve reserves invoice estimate inspection notice exclusion
    exclusions excluded endorsement sublimit sublimits edition guideline guidelines procedure process
    escalate report roof flood wording form deadline grace region regions state states channel
    water wind hail fire theft mold north south east west
    minnesota wisconsin texas oklahoma pennsylvania ohio colorado arizona
"""

_STOPWORDS = {"how", "many", "the", "and", "our", "new", "all", "does", "say"}


def screen(question: str) -> Refusal | None:
    text = _normalize(question)
    if not text:
        return Refusal("empty", "Ask a question about claims, policies, or payments and I will look it up.")
    if len(text) > MAX_LENGTH:
        return Refusal(
            "too_long",
            "That question is longer than I can take in. Send a shorter one about a single claim, figure, or "
            "policy point.",
        )
    if _INJECTION.search(text):
        return Refusal(
            "injection",
            "I can only answer questions about the claims data you are cleared to see, such as a claim, "
            "a figure like paid losses, or what a policy says.",
        )
    if _CODING.search(text):
        return Refusal(
            "coding",
            "I answer questions about the claims data, not requests to write code. Ask about a claim, a figure, or "
            "a policy point.",
        )
    if _CHIT_CHAT.search(text):
        return Refusal(
            "chit_chat",
            "I am here for claims questions. Ask about a specific claim, a figure like paid losses, or "
            "what the policy covers.",
        )
    if _OFF_TOPIC_CUES.search(text) and not _has_domain_term(text):
        return Refusal(
            "off_topic",
            "I only cover claims, policies, and payments. Try a claim, a loss figure, or a coverage question.",
        )
    return None


def _has_domain_term(text: str) -> bool:
    words = set(re.findall(r"[a-z]{2,}", text))
    return bool(words & _domain_vocabulary())
