from collections.abc import Sequence
from typing import Any

from app.config import policy
from evals.outcome import Outcome, Verdict, rate
from tests.answer.test_why_answers import driver_words


def score_answers(outcomes: Sequence[Outcome]) -> tuple[dict[str, Any], list[Verdict]]:
    """Qualitative answers. One is grounded when a sentence the verifier kept cites a passage labelled relevant."""
    facts = policy()["facts"]
    grounded = found = wanted = cut = claims = 0
    verdicts = []
    for o in outcomes:
        answer = o.answer if o.answered else None
        cited = {hit.anchor for hit in answer.citations} if answer else set()
        ok = bool(cited & set(o.case["relevant"]))
        grounded += ok
        text = answer.text.lower() if answer else ""
        for fact in o.case["facts"]:
            wanted += 1
            found += str(facts[fact]).lower() in text
        if o.answer is not None:
            cut += len(o.answer.claims_cut)
            claims += len(o.answer.claims_kept) + len(o.answer.claims_cut)
        verdicts.append(Verdict(o.answered, ok and o.label == o.case["route"]))
    n = len(outcomes)
    section = {
        "answered": rate(sum(v.answered for v in verdicts), n),
        "grounded": rate(grounded, n),
        "fact_recall": rate(found, wanted),
        "claims_cut": rate(cut, claims),
    }
    return section, verdicts


def score_why(outcomes: Sequence[Outcome]) -> tuple[dict[str, Any], list[Verdict]]:
    """Why answers. The driver is named when the answer names every word tests/answer uses for it (its peril,
    state and regions); the citation counts when a cited passage comes from a document the case expects."""
    named = cited = 0
    verdicts = []
    for o in outcomes:
        answer = o.answer if o.answered else None
        text = answer.text.lower() if answer else ""
        says = answer is not None and all(word.lower() in text for word in driver_words(o.case["driver"]))
        cites = answer is not None and any(hit.doc_id in o.case["cite"] for hit in answer.citations)
        named += says
        cited += cites
        verdicts.append(Verdict(o.answered, says and cites and o.label == o.case["route"]))
    n = len(outcomes)
    section = {
        "answered": rate(sum(v.answered for v in verdicts), n),
        "driver_named": rate(named, n),
        "cited": rate(cited, n),
    }
    return section, verdicts
