from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from app import events as ev
from app.answer.format import ref
from app.answer.lookup import answer_lookup
from app.answer.mentions import claim_id_in
from app.answer.qual import answer_qual
from app.answer.quant import answer_quant
from app.answer.scanfield import answer_scan, asks_about_scan
from app.answer.types import Claim, Draft, Evidence
from app.answer.why import answer_why
from app.identity import Principal
from app.memory import LastTurn
from app.semantic.layer import default_layer
from app.sources.documents import Hit

GENERIC_CLARIFY = ev.Clarify(
    "Which figure and period do you mean?",
    ("paid losses in 2025", "claims reported last quarter", "denial rate by region in 2025"),
)
CAPABILITIES = ev.Clarify(
    "I can look up a claim, give figures such as paid losses or claim counts, say what a policy or guideline"
    " says, and explain why a figure changed. Which would help?",
    # Never a real claim number here: every user sees these, and a claim id outside their region is a leak.
    (
        "Paid losses in the West in Q2 2025",
        "Is flood damage covered?",
        "Why were paid losses in the West so high in Q2 2025?",
    ),
)
WHICH_CLAIM = ev.Clarify("Which claim do you mean? Give me its number.", ())
LOOKUP_MONEY = ("paid_total", "reserve", "deductible")


@dataclass
class Handled:
    """What a route produced: evidence to show, then either a draft to verify, a plain text, or a terminal event."""

    outcome: ev.Outcome
    events: list[ev.Event] = field(default_factory=list)
    draft: Draft | None = None
    evidence: Evidence | None = None
    text: str | None = None
    turn: LastTurn | None = None
    claim_ids: set[int] = field(default_factory=set)
    doc_ids: set[str] = field(default_factory=set)


Handler = Callable[[Principal, str, LastTurn | None], Awaitable[Handled]]


def _read(hits: Iterable[Hit]) -> tuple[set[int], set[str]]:
    listed = list(hits)
    return {h.claim_id for h in listed if h.claim_id is not None}, {h.doc_id for h in listed}


def _sql(statement: str | None, params: Iterable[Any] = ()) -> list[ev.Event]:
    return [ev.Evidence("sql", {"sql": statement, "params": list(params)})] if statement else []


async def lookup(principal: Principal, question: str, previous: LastTurn | None) -> Handled:
    claim_id = claim_id_in(question) or (previous.claim_id if previous else None)
    if claim_id is None:
        return Handled("clarify", [WHICH_CLAIM])
    turn = LastTurn("lookup", question, claim_id=claim_id)
    if asks_about_scan(question):
        scan = await answer_scan(principal, question, claim_id)
        if scan.kind == "clarify":
            return Handled("clarify", [ev.Clarify(scan.text, scan.options)], turn=turn)
        if scan.kind not in ("found", "flagged"):
            return Handled("not_allowed" if scan.kind == "not_allowed" else "not_found", text=scan.text, turn=turn)
        fields = tuple(asdict(f) for f in scan.fields)
        rows = ({"paid": scan.paid},) if scan.paid is not None else ()
        events = [*_sql(scan.sql, [claim_id]), ev.Evidence("scan", list(fields))]
        read_docs = {scan.doc_id} if scan.doc_id else set()
        evidence = Evidence(rows, (), (), fields)
        return Handled("answer", events, scan.draft, evidence, turn=turn, claim_ids={claim_id}, doc_ids=read_docs)
    found = await answer_lookup(principal, claim_id)
    if found.kind != "found":
        return Handled("not_allowed" if found.kind == "not_allowed" else "not_found", text=found.text, turn=turn)
    numbers = tuple(ref(found.fields[c], "currency", c, 0, c) for c in LOOKUP_MONEY if found.fields.get(c) is not None)
    draft = Draft((Claim(found.text, numbers, ()),), ())
    events = [*_sql(found.sql, [claim_id]), ev.Evidence("rows", [found.fields])]
    return Handled("answer", events, draft, Evidence((found.fields,), (), (), ()), turn=turn, claim_ids={claim_id})


async def quantitative(principal: Principal, question: str, previous: LastTurn | None) -> Handled:
    result = await answer_quant(principal, question, previous.query if previous else None, layer=default_layer())
    turn = LastTurn("quantitative", question, result.query)
    if result.kind == "clarify" and result.clarify is not None:
        return Handled("clarify", [ev.Clarify(result.clarify.question, result.clarify.options)], turn=turn)
    if result.kind == "out_of_data":
        return Handled("out_of_data", [ev.OutOfData(result.text, default_layer().coverage.label)], turn=turn)
    if result.kind != "answer":
        return Handled("not_allowed" if result.kind == "not_allowed" else "unavailable", text=result.text, turn=turn)
    rows = tuple(dict(zip(result.columns, row, strict=True)) for row in result.rows)
    # One claim per line, so a figure the verifier can't trace cuts only its own line of a breakdown.
    lines = [line for line in result.text.split("\n") if line]
    draft = Draft(tuple(Claim(line, result.numbers, ()) for line in lines), ())
    events = [*_sql(result.sql, result.params), ev.Evidence("rows", list(rows))]
    return Handled("answer", events, draft, Evidence(rows, (), (), ()), turn=turn)


async def qualitative(principal: Principal, question: str, previous: LastTurn | None) -> Handled:
    result = await answer_qual(principal, question)
    claim_ids, doc_ids = _read(result.hits)
    turn = LastTurn("qualitative", question, claim_id=result.claim_id)
    events: list[ev.Event] = [ev.Evidence("chunks", list(result.hits))]
    if result.kind != "answer":
        outcome: ev.Outcome = "not_allowed" if result.kind == "not_allowed" else "not_found"
        return Handled(outcome, events, text=result.text, turn=turn, claim_ids=claim_ids, doc_ids=doc_ids)
    return Handled("answer", events, result.draft, result.evidence, None, turn, claim_ids, doc_ids)


async def why(principal: Principal, question: str, previous: LastTurn | None) -> Handled:
    result = await answer_why(principal, question, previous.query if previous else None)
    turn = LastTurn("why", question, result.query)
    if result.kind == "clarify" and result.clarify is not None:
        return Handled("clarify", [ev.Clarify(result.clarify.question, result.clarify.options)], turn=turn)
    if result.kind == "out_of_data":
        covered = (result.covered or default_layer().coverage).label
        return Handled("out_of_data", [ev.OutOfData(result.text, covered)], turn=turn)
    if not result.draft.claims:
        return Handled("not_allowed" if result.kind == "not_allowed" else "answer", text=result.text, turn=turn)
    evidence = result.evidence
    claim_ids, doc_ids = _read(evidence.hits)
    events: list[ev.Event] = [ev.Evidence("sql", {"sql": statement, "params": []}) for statement in result.sql]
    events += [ev.Evidence("rows", list(evidence.rows)), ev.Evidence("sandbox", list(evidence.sandbox))]
    events.append(ev.Evidence("chunks", list(evidence.hits)))
    return Handled("answer", events, result.draft, evidence, None, turn, claim_ids, doc_ids)


async def clarify(principal: Principal, question: str, previous: LastTurn | None) -> Handled:
    result = await answer_quant(principal, question, previous.query if previous else None, layer=default_layer())
    if result.kind == "clarify" and result.clarify is not None:
        # Remembered as a quantitative turn holding what was understood, so the reply fills in the gap.
        turn = LastTurn("quantitative", question, result.clarify.partial or result.query)
        return Handled("clarify", [ev.Clarify(result.clarify.question, result.clarify.options)], turn=turn)
    return Handled("clarify", [GENERIC_CLARIFY])


async def out_of_data(principal: Principal, question: str, previous: LastTurn | None) -> Handled:
    result = await answer_quant(principal, question, previous.query if previous else None, layer=default_layer())
    covered = default_layer().coverage.label
    if result.kind == "out_of_data":
        return Handled("out_of_data", [ev.OutOfData(result.text, covered)])
    text = f"I can only answer from the data, which covers {covered}, so I can't forecast or reach outside it."
    return Handled("out_of_data", [ev.OutOfData(text, covered)])


async def residue(principal: Principal, question: str, previous: LastTurn | None) -> Handled:
    return Handled("clarify", [CAPABILITIES])


HANDLERS: dict[str, Handler] = {
    "lookup": lookup,
    "quantitative": quantitative,
    "qualitative": qualitative,
    "why": why,
    "clarify": clarify,
    "out_of_data": out_of_data,
    "residue": residue,
}
