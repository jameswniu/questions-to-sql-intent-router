"""Builds the planted-error set for the verifier.

Clean drafts come from the quantitative workflow run as real users on dev questions, and each mutated copy carries
one planted error. Needs the compose database: uv run python -m evals.verifier.build
"""

import asyncio
import json
import re
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app import db
from app.answer.figures import Form, Written, figures
from app.answer.format import NumberRef, format_ratio, format_value
from app.answer.quant import QuantResult, answer_quant
from app.answer.types import Claim, Draft, Evidence
from app.config import ROOT
from app.identity import Principal, principal_for, principals
from app.semantic.layer import default_layer
from app.sources import documents
from evals.verifier.planted import PLANTED, Case, dump

QUESTIONS = ROOT / "evals" / "cases" / "quantitative.jsonl"
# Two dev questions per kind of error, each with the shape its error needs: a compare answer for a percent change,
# a direction or a multiple, a grouped answer for a swap, a superlative or a region, a monthly series for a period,
# a single figure for a constant, an adjuster for a chunk from another region. The dev set has two compare answers,
# so those three kinds share them.
PLAN = {
    "digit": ("quant-001", "quant-035"),
    "percent": ("quant-006", "quant-023"),
    "swap": ("quant-010", "quant-018"),
    "percent_change": ("quant-004", "quant-027"),
    "unretrieved_source": ("quant-008", "quant-002"),
    "other_region_source": ("quant-013", "quant-015"),
    "unsupported_sentence": ("quant-003", "quant-021"),
    "rounding": ("quant-005", "quant-029"),
    "direction": ("quant-004", "quant-027"),
    "superlative": ("quant-016", "quant-033"),
    "multiple": ("quant-004", "quant-027"),
    "region": ("quant-010", "quant-032"),
    "period": ("quant-024", "quant-026"),
    "constant": ("quant-019", "quant-011"),
}
SENTENCE = re.compile(r"(?<=\.)\s+(?=[A-Z])")
# The compare sentence the workflow writes: "Paid losses were $35,768,928 in 2025 and $24,345,938 in 2024, up ..."
COMPARED = re.compile(r"^(?P<subject>.+?) (?:was|were) .+? in (?P<now>.+?) and .+? in (?P<then>.+?), (?P<way>up|down) ")
# The workflow's direction word, then the clean and the mutated way to say the change as a noun.
AS_NOUN = {", up ": (", an increase of ", ", a decrease of "), ", down ": (", a decrease of ", ", an increase of ")}
# The grouped summary: "Claim count in 2025 was highest for West at 1,001 and lowest for East at 569."
RANKED = re.compile(r"highest for (?P<top>\w+) at .+ and lowest for (?P<bottom>\w+) at ")
# The series summary: "... ranged from 9 in Jul 2025 to 314 in Jan 2025."
RANGED = re.compile(r" ranged from (?P<low>\S+) in (?P<first>.+?) to (?P<high>\S+) in (?P<last>.+?)\.$")
Planted = tuple[Draft, Draft, tuple[int, ...]]  # the clean draft, its mutated copy, the claims carrying the error


def _shows(display: str, text: str) -> bool:
    return re.search(rf"(?<![\d,.$]){re.escape(display)}(?![\d,]|\.\d)", text) is not None


def draft_from(result: QuantResult) -> Draft:
    """Each bullet, and each sentence that shows a figure, becomes a claim carrying the refs it shows."""
    claims: list[Claim] = []
    caveats: list[str] = []
    for line in result.text.split("\n"):
        for piece in [line] if line.startswith("- ") else SENTENCE.split(line):
            refs = tuple(dict.fromkeys(ref for ref in result.numbers if _shows(ref.display, piece)))
            if refs:
                claims.append(Claim(piece, refs, ()))
            else:
                caveats.append(piece)
    return Draft(tuple(claims), tuple(caveats))


async def _evidence(result: QuantResult, principal: Principal, question: str) -> Evidence:
    rows = tuple(dict(zip(result.columns, row, strict=True)) for row in result.rows)
    # Lexical, because the embedding models ship only in the app image and this runs on the host too.
    hits = tuple(await documents.search(principal, question, k=3, mode="lexical"))
    return Evidence(rows, hits, (), ())


def _edit(draft: Draft, index: int, **changes: Any) -> Draft:
    claims = list(draft.claims)
    claims[index] = replace(claims[index], **changes)
    return replace(draft, claims=tuple(claims))


def _retext(draft: Draft, index: int, old: str, new: str) -> Draft:
    return _edit(draft, index, text=draft.claims[index].text.replace(old, new, 1))


def _first(draft: Draft, *forms: Form) -> tuple[int, Written]:
    return next((i, w) for i, claim in enumerate(draft.claims) for w in figures(claim.text) if w.form in forms)


def change_digit(draft: Draft) -> Planted:
    index, written = _first(draft, "dollars")
    at = [i for i, ch in enumerate(written.token) if ch.isdigit()][-2]
    changed = written.token[:at] + str((int(written.token[at]) + 4) % 10) + written.token[at + 1 :]
    return draft, _retext(draft, index, written.token, changed), (index,)


def shift_percent(draft: Draft) -> Planted:
    index, written = _first(draft, "percent")
    return draft, _retext(draft, index, written.token, f"{written.value + 2:.{written.places}f}%"), (index,)


def swap_figures(draft: Draft) -> Planted:
    a, b = [i for i, claim in enumerate(draft.claims) if claim.text.startswith("- ")][:2]
    one, two = figures(draft.claims[a].text)[0].token, figures(draft.claims[b].text)[0].token
    return draft, _retext(_retext(draft, a, one, two), b, two, one), (a, b)


def misstate_change(draft: Draft) -> Planted:
    """The change taken over the new value instead of the old one, still recorded as a change over the old one."""
    index, claim = next((i, c) for i, c in enumerate(draft.claims) if any(r.row_index is None for r in c.numbers))
    pct = next(r for r in claim.numbers if r.row_index is None and r.display.endswith("%"))
    delta = next(r for r in claim.numbers if r.row_index is None and r is not pct)
    now = next(r for r in claim.numbers if r.row_index == 0)
    wrong = delta.value / abs(now.value)
    bad = NumberRef(wrong, format_ratio(wrong), pct.column, None, pct.derivation)
    numbers = tuple(bad if r is pct else r for r in claim.numbers)
    return draft, _edit(draft, index, text=claim.text.replace(pct.display, bad.display, 1), numbers=numbers), (index,)


def swap_source(draft: Draft, evidence: Evidence, chunk_id: str) -> Planted:
    clean = _edit(draft, 0, citations=(evidence.hits[0].chunk_id,)) if evidence.hits else draft
    return clean, _edit(draft, 0, citations=(chunk_id,)), (0,)


async def _unretrieved(principal: Principal, evidence: Evidence) -> str:
    """A chunk the asker can read that this question didn't retrieve."""
    seen = {hit.chunk_id for hit in evidence.hits}
    found = await documents.search(principal, "duties after a loss and the deductible", k=10, mode="lexical")
    return next(hit.chunk_id for hit in found if hit.chunk_id not in seen)


async def _other_region(principal: Principal, question: str) -> str:
    """A real claim note from a region the asker can't see, found by asking as an adjuster there."""
    for other in sorted(principals().values(), key=lambda p: p.user_id):
        if other.kind != "adjuster" or set(other.regions) & set(principal.regions):
            continue
        for hit in await documents.search(other, question, k=5, kinds=("note",), mode="lexical"):
            if hit.region is not None and hit.region not in principal.regions:
                return hit.chunk_id
    raise LookupError(f"no note outside {principal.regions} answers {question!r}")


def add_unsupported(draft: Draft, with_ref: bool) -> Planted:
    """A sentence whose figure nothing in the evidence holds, with or without a ref that claims it does."""
    first = draft.claims[0].numbers[0]
    made_up = (first.value * Decimal("0.07")).quantize(Decimal(1))
    shown = format_value(made_up, "currency")
    refs = (NumberRef(made_up, shown, first.column, None, first.column),) if with_ref else ()
    extra = Claim(f"Reopened claims made up {shown} of that.", refs, ())
    return draft, replace(draft, claims=(draft.claims[0], extra, *draft.claims[1:])), (1,)


def _rounded(value: Decimal, off: int) -> str:
    """In millions or thousands to one decimal, moved off tenths away from the true value."""
    scale, unit = (Decimal(1_000_000), " million") if abs(value) >= 1_000_000 else (Decimal(1_000), "K")
    exact = value / scale
    shown = exact.quantize(Decimal("0.1"), ROUND_HALF_UP)
    shown += Decimal("0.1") * off * (1 if shown >= exact else -1)
    return f"${shown}{unit}"


def misround(draft: Draft) -> Planted:
    """The clean copy rounds a dollar figure correctly to millions or thousands; the mutated one a tenth off."""
    index, written = _first(draft, "dollars")
    value = next(ref.value for ref in draft.claims[index].numbers if ref.display == written.token)
    correct, wrong = _rounded(value, 0), _rounded(value, 1)
    return _retext(draft, index, written.token, correct), _retext(draft, index, written.token, wrong), (index,)


def flip_direction(draft: Draft) -> Planted:
    """The change said as an increase or a decrease: the clean copy names the way it went, the mutated one the other."""
    index, moved = next((i, w) for i, claim in enumerate(draft.claims) for w in AS_NOUN if w in claim.text)
    right, wrong = AS_NOUN[moved]
    return _retext(draft, index, moved, right), _retext(draft, index, moved, wrong), (index,)


def swap_superlative(draft: Draft) -> Planted:
    """The mutated copy calls the highest group lowest and the lowest highest, each beside its own figure."""
    index, claim = next(
        (i, c) for i, c in enumerate(draft.claims) if " highest for " in c.text and " lowest " in c.text
    )
    swapped = re.sub(r"\b(highest|lowest)\b", lambda m: "lowest" if m[1] == "highest" else "highest", claim.text)
    return draft, _edit(draft, index, text=swapped), (index,)


def misstate_multiple(draft: Draft) -> Planted:
    """A sentence restating the change, resting on the change refs of the sentence before it: the clean copy says
    which way it went, the mutated one that it doubled."""
    index, found = next((i, m) for i, claim in enumerate(draft.claims) if (m := COMPARED.match(claim.text)))
    change = tuple(ref for ref in draft.claims[index].numbers if ref.row_index is None)
    pct = next(ref for ref in change if ref.display.endswith("%"))
    if abs(pct.value - 1) < Decimal("0.5"):
        raise ValueError(f"{draft.claims[index].text!r} is close enough to double that doubled wouldn't be an error")

    def restated(verb: str) -> Draft:
        extra = Claim(f"{found['subject']} {verb} from {found['then']} to {found['now']}.", change, ())
        return replace(draft, claims=(*draft.claims[: index + 1], extra, *draft.claims[index + 1 :]))

    return restated("rose" if found["way"] == "up" else "fell"), restated("doubled"), (index + 1,)


def swap_region(draft: Draft) -> Planted:
    """Both copies name the top and bottom regions as "the West"; the mutated one gives each the other's figure."""
    index, found = next((i, m) for i, claim in enumerate(draft.claims) if (m := RANKED.search(claim.text)))
    top, bottom = found["top"], found["bottom"]
    clean = draft.claims[index].text.replace(f"for {top} at", f"for the {top} at")
    clean = clean.replace(f"for {bottom} at", f"for the {bottom} at")
    swapped = re.sub(rf"the ({top}|{bottom}) at", lambda m: f"the {bottom if m[1] == top else top} at", clean)
    return _edit(draft, index, text=clean), _edit(draft, index, text=swapped), (index,)


def swap_period(draft: Draft) -> Planted:
    """The mutated copy gives the lowest and the highest figure of a series each other's month or quarter."""
    index, found = next((i, m) for i, claim in enumerate(draft.claims) if (m := RANGED.search(claim.text)))
    text = draft.claims[index].text
    swapped = (
        f"{text[: found.start()]} ranged from {found['low']} in {found['last']} to {found['high']} in {found['first']}."
    )
    return draft, _edit(draft, index, text=swapped), (index,)


def constant_figure(draft: Draft) -> Planted:
    """The mutated copy states a figure the evidence doesn't hold, recorded as a literal that comes to itself."""
    index, claim = next((i, c) for i, c in enumerate(draft.claims) if c.numbers)
    real = claim.numbers[0]
    made_up = (real.value * Decimal("1.07")).quantize(Decimal(1))
    shown = format_value(made_up, "currency" if real.display.startswith("$") else "integer")
    literal = NumberRef(made_up, shown, real.column, real.row_index, str(made_up))
    text = claim.text.replace(real.display, shown, 1)
    return draft, _edit(draft, index, text=text, numbers=(literal, *claim.numbers[1:])), (index,)


async def _plant(
    mutation: str, second: bool, draft: Draft, evidence: Evidence, principal: Principal, q: str
) -> Planted:
    match mutation:
        case "digit":
            return change_digit(draft)
        case "percent":
            return shift_percent(draft)
        case "swap":
            return swap_figures(draft)
        case "percent_change":
            return misstate_change(draft)
        case "unretrieved_source":
            return swap_source(draft, evidence, await _unretrieved(principal, evidence))
        case "other_region_source":
            return swap_source(draft, evidence, await _other_region(principal, q))
        case "unsupported_sentence":
            return add_unsupported(draft, with_ref=second)
        case "rounding":
            return misround(draft)
        case "direction":
            return flip_direction(draft)
        case "superlative":
            return swap_superlative(draft)
        case "multiple":
            return misstate_multiple(draft)
        case "region":
            return swap_region(draft)
        case "period":
            return swap_period(draft)
        case "constant":
            return constant_figure(draft)
    raise ValueError(f"unknown mutation {mutation!r}")


async def build() -> tuple[Case, ...]:
    questions = {case["id"]: case for case in map(json.loads, QUESTIONS.read_text().splitlines())}
    layer = default_layer()
    cases = []
    for mutation, ids in PLAN.items():
        for n, qid in enumerate(ids):
            question = questions[qid]
            if question["split"] != "dev":
                raise ValueError(f"{qid} is not a dev question")
            principal = principal_for(question["user"])
            result = await answer_quant(principal, question["q"], None, layer=layer)
            if result.kind != "answer":
                raise RuntimeError(f"{qid} came back {result.kind}: {result.text}")
            evidence = await _evidence(result, principal, question["q"])
            clean, mutated, planted = await _plant(
                mutation, n == 1, draft_from(result), evidence, principal, question["q"]
            )
            # A question can serve more than one kind of error, so a clean case is named for the error it pairs with.
            cases.append(Case(f"{qid}-{mutation}-clean", qid, question["user"], "clean", (), clean, evidence))
            cases.append(Case(f"{qid}-{mutation}", qid, question["user"], mutation, planted, mutated, evidence))
    return tuple(cases)


async def main() -> None:
    try:
        cases = await build()
    finally:
        await db.close_all()
    dump(cases)
    print(f"wrote {len(cases)} cases to {PLANTED.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
