import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Literal

from app import db
from app.answer import drivers as dv
from app.answer.format import NumberRef, as_decimal, change_refs, format_ratio, long_date, ref
from app.answer.mentions import issued_on
from app.answer.passages import SAME_CHUNK_MARGIN, Passage, choose, score
from app.answer.quant import scope
from app.answer.types import Claim, Draft, Evidence
from app.identity import Principal
from app.ingest import embed
from app.sandbox.client import SandboxUnavailable
from app.semantic.describe import display_name, is_plural, state_names, subject
from app.semantic.extract import extract
from app.semantic.layer import Layer, default_layer
from app.semantic.query import MONTHS, Clarify, MetricQuery, OutOfData, Period
from app.semantic.resolve import resolve
from app.sources.documents import Hit, search

Kind = Literal["answer", "clarify", "out_of_data", "not_allowed"]
DOC_KINDS = ("memo", "bulletin")
DOC_K = 8
# A memo or bulletin explains a period when it was issued in it or within about a quarter either side.
WINDOW_DAYS = 92
LEAD_SQL = "SELECT DISTINCT ON (doc_id) doc_id, body FROM rag.chunks WHERE doc_id = ANY(%s) ORDER BY doc_id, ordinal"
QUARTERS = ("first", "second", "third", "fourth")
RISE_WORDS = "rise rose raised increase higher more larger above"
FALL_WORDS = "fall fell dropped decrease lower fewer smaller below"
NO_DOCUMENT = "I found no memo or bulletin from around then that explains it."
NO_SPLIT = "I couldn't split the change by driver this time."


@dataclass(frozen=True)
class WhyResult:
    kind: Kind
    text: str
    draft: Draft
    evidence: Evidence
    query: MetricQuery | None = None
    clarify: Clarify | None = None
    covered: Period | None = None
    sql: tuple[str, ...] = ()
    # The values bound to each statement's placeholders, in the order of sql. A result that doesn't keep them leaves
    # this empty, and its statements show without their values.
    params: tuple[tuple[Any, ...], ...] = ()

    @property
    def statements(self) -> list[dv.Ran]:
        """Each statement in sql with the values bound to it, or none where they weren't kept."""
        return [(sql, self.params[i] if i < len(self.params) else ()) for i, sql in enumerate(self.sql)]


def _result(kind: Kind, text: str, **extra: Any) -> WhyResult:
    return WhyResult(kind, text, Draft((), ()), Evidence((), (), (), ()), **extra)


def _label(group: dv.Group) -> str:
    if group.dim == "state":
        return state_names().get(group.key, group.key)
    return f"the {group.key}" if group.dim == "region" else f"{group.key} claims"


def _driver_claim(chosen: list[tuple[dv.Group, ...]], rising: bool, change: tuple[NumberRef, ...]) -> Claim:
    """change is the headline's change, which the sentence's "rise" or "fall" names, so it carries those refs too."""
    direction = "rise" if rising else "fall"
    first = chosen[0]
    refs = [g.ref for top in chosen for g in top]
    lead = _label(first[0])
    verb = "account" if lead.endswith(" claims") else "accounts"
    text = f"{lead[0].upper()}{lead[1:]} {verb} for {refs[0].display} of the {direction}"
    if len(refs) > 1:
        joiner = " and" if len(first) == 2 else ", and"
        text += f"{joiner} {_label(chosen[-1][-1])} for {refs[1].display}"
    return Claim(f"{text}.", (*refs, *change), ())


def _effects_claim(
    result: dict[str, Any], mq: MetricQuery, layer: Layer, rising: bool, change: tuple[NumberRef, ...]
) -> Claim | None:
    """For a total whose every dimension the question pins: whether more claims or bigger ones moved it. Like the
    driver sentence, it carries the headline's change refs for the "rise" or "fall" it names."""
    count_measure = dv.driver_specs().get(mq.measure, {}).get("count")
    delta = as_decimal(result.get("delta_total") or 0)
    if layer.measures[mq.measure].kind != "sum" or not count_measure or not delta:
        return None
    direction = "rise" if rising else "fall"
    count, mean = as_decimal(result["count_effect"]), as_decimal(result["mean_effect"])
    if abs(count) >= abs(mean):
        share = count / delta
        number = NumberRef(share, format_ratio(share), "count_effect", None, "count_effect / delta_total")
        label = layer.measures[count_measure].label
        text = f"{'More' if count > 0 else 'Fewer'} {label} account for {number.display} of the {direction}."
    else:
        share = mean / delta
        number = NumberRef(share, format_ratio(share), "mean_effect", None, "mean_effect / delta_total")
        size = "larger" if mean > 0 else "smaller"
        text = f"A {size} amount per claim accounts for {number.display} of the {direction}."
    return Claim(text, (number, *change), ())


def _period_words(period: Period) -> str:
    if period.months == 3 and period.start.month % 3 == 1:
        months = " ".join(MONTHS[period.start.month - 1 + i] for i in range(3))
        return f"{period.label} {QUARTERS[period.start.month // 3]} quarter {period.start.year} {months}"
    return period.label


def _doc_query(mq: MetricQuery, layer: Layer, chosen: list[tuple[dv.Group, ...]]) -> str:
    measure = layer.measures[mq.measure]
    words = [measure.label, display_name(measure), *mq.filters.get("peril", ()), *mq.filters.get("region", ())]
    words += [state_names().get(s, s) for s in mq.filters.get("state", ())]
    words += [_label(g).removeprefix("the ").removesuffix(" claims") for top in chosen for g in top]
    assert mq.period is not None
    words.append(_period_words(mq.period))
    return " ".join(dict.fromkeys(words))


async def _issue_dates(principal: Principal, doc_ids: set[str]) -> dict[str, Any]:
    if not doc_ids:
        return {}
    found = await db.run(principal, LEAD_SQL, (sorted(doc_ids),))
    return {doc_id: issued_on(body) for doc_id, body in found.rows}


async def _explaining(
    principal: Principal, question: str, query: str, context: str, period: Period
) -> tuple[list[Passage], tuple[Hit, ...]]:
    """Sentences from the memo or bulletin dated around the period that best explains the change."""
    hits = [hit for hit in await search(principal, query, k=DOC_K, kinds=DOC_KINDS) if not hit.quarantined]
    issued = await _issue_dates(principal, {hit.doc_id for hit in hits})
    start, end = period.start - timedelta(days=WINDOW_DAYS), period.end + timedelta(days=WINDOW_DAYS)
    kept = tuple(hit for hit in hits if (day := issued.get(hit.doc_id)) and start <= day <= end)
    if not kept:
        return [], ()
    same = [hit for hit in kept if hit.doc_id == kept[0].doc_id]
    scored = await asyncio.to_thread(score, same, question, context=f"{query} {context}", rerank=embed.rerank)
    return choose(scored, most=2, least=1, other_margin=SAME_CHUNK_MARGIN, judge=False), kept


def _headline(
    mq: MetricQuery, layer: Layer, now: dv.Side, then: dv.Side, agg: bool
) -> tuple[Claim, tuple[NumberRef, ...]]:
    """The headline sentence, and its change and percent change refs."""
    measure = layer.measures[mq.measure]
    assert mq.period is not None and now.value is not None and then.value is not None
    ratio = measure.kind == "ratio"
    column, source = ("num", "num / den" if ratio else "num") if agg else ("value", "value")
    if ratio and not agg:
        source = "numerator / denominator"
    current = ref(now.value, measure.format, column, now.row, source)
    prior = ref(then.value, measure.format, column, then.row, source)
    changes = change_refs(now.value, then.value, measure.format, (now.row, then.row), source)
    before = mq.period.prior()
    moved = "unchanged"
    if now.value != then.value:
        pct = f" ({changes[1].display})" if len(changes) > 1 else ""
        moved = f"{'up' if now.value > then.value else 'down'} {changes[0].display}{pct}"
    verb = "were" if is_plural(measure) else "was"
    text = (
        f"{subject(mq, layer)} {verb} {current.display} in {mq.period.label}"
        f" and {prior.display} in {before.label}, {moved}."
    )
    return Claim(text, (current, prior, *changes), ()), changes


async def _split(
    principal: Principal,
    mq: MetricQuery,
    layer: Layer,
    decompose: dv.Decompose,
    now: dv.Side,
    then: dv.Side,
    offset: int,
) -> tuple[list[tuple[dv.Group, ...]], list[dict[str, Any]], list[dict[str, Any]], list[dv.Ran]]:
    ratio = layer.measures[mq.measure].kind == "ratio"
    agg = principal.kind == "analyst"
    per_dimension: list[list[dv.Group]] = []
    rows: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    ran: list[dv.Ran] = []
    dims = dv.free_dimensions(mq, layer, analyst=agg)
    if not dims:
        whole = await dv.whole_table(principal, mq, layer, now, then)
        result = await decompose(principal.db_role, whole.table, dv.decompose_params(None))
        return [], whole.records, [result], whole.ran
    # One dimension at a time: the daemon runs two jobs per login at most, and each is quick.
    for dim in dims:
        split = await dv.group_table(principal, mq, dim, layer)
        result = await decompose(principal.db_role, split.table, dv.decompose_params(dim))
        per_dimension.append(
            dv.contributions(result, split, offset + len(rows), ratio=ratio, agg=agg, current=now, prior=then)
        )
        rows += split.records
        ran += split.ran
        results.append(result)
    return dv.pick_drivers(per_dimension), rows, results, ran


async def answer_why(
    principal: Principal,
    question: str,
    previous: MetricQuery | None = None,
    *,
    layer: Layer | None = None,
    decompose: dv.Decompose = dv.in_sandbox,
) -> WhyResult:
    layer = layer or default_layer()
    planned = plan_why(principal, question, previous, layer)
    if isinstance(planned, WhyResult):
        return planned
    whole, note = planned
    return await _explain(principal, question, whole, layer, decompose, note)


def plan_why(
    principal: Principal, question: str, previous: MetricQuery | None, layer: Layer
) -> WhyResult | tuple[MetricQuery, str | None]:
    """The change a why question asks about, scoped to the user, with the note scoping adds, or the answer that needs
    no change explained: a clarifying question, an out-of-data answer, a snapshot measure or a region the user can't
    see. Live mode plans the same way, so it explains exactly the questions this workflow would."""
    extracted = extract(question, layer, previous)
    if isinstance(extracted, Clarify):
        return _result("clarify", extracted.question, clarify=extracted, query=extracted.partial)
    resolved = resolve(extracted, layer, analyst=principal.kind == "analyst")
    if isinstance(resolved, Clarify):
        return _result("clarify", resolved.question, clarify=resolved, query=resolved.partial)
    if isinstance(resolved, OutOfData):
        return _result("out_of_data", resolved.reason, covered=resolved.covered, query=extracted)
    measure = layer.measures[resolved.measure]
    if measure.point_in_time or resolved.period is None:
        verb = "are" if is_plural(measure) else "is"
        text = f"{display_name(measure).capitalize()} {verb} a snapshot, so there is no change over time to explain."
        return _result("not_allowed", text, query=resolved)
    before = resolved.period.prior()
    if before.start < layer.coverage.start:
        start = long_date(layer.coverage.start)
        text = f"The data starts on {start}, so there is nothing before {resolved.period.label} to compare it with."
        return _result("out_of_data", text, covered=layer.coverage, query=resolved)
    scoped, note = scope(resolved, principal, layer)
    if scoped is None:
        return _result("not_allowed", note or "", query=resolved)
    return replace(scoped, group_by=(), grain=None, limit=None), note


async def _explain(
    principal: Principal,
    question: str,
    mq: MetricQuery,
    layer: Layer,
    decompose: dv.Decompose,
    note: str | None,
) -> WhyResult:
    assert mq.period is not None
    headline = await dv.fetch(principal, replace(mq, compare_to="prior_period"), layer)
    figures = dv.sides(headline, layer.measures[mq.measure].kind == "ratio")
    now, then = figures.get("current"), figures.get("prior")
    rows, ran = headline.records(), [headline.ran]
    caveats = [note] if note else []
    if now is None or then is None or now.value is None or then.value is None:
        what = subject(mq, layer, capital=False)
        text = f"There isn't enough data on {what} to compare {mq.period.label} with the period before."
        return _result("answer", text, query=mq, sql=(headline.sql,), params=(headline.params,))
    head, change = _headline(mq, layer, now, then, "grp" in headline.columns)
    claims = [head]
    chosen: list[tuple[dv.Group, ...]] = []
    results: list[dict[str, Any]] = []
    if now.value != then.value:
        try:
            chosen, split_rows, results, split_ran = await _split(principal, mq, layer, decompose, now, then, len(rows))
            rows, ran = rows + split_rows, ran + split_ran
        except (dv.SandboxFailed, SandboxUnavailable):
            caveats.append(NO_SPLIT)
        if chosen:
            claims.append(_driver_claim(chosen, now.value > then.value, change))
        elif results and (effects := _effects_claim(results[0], mq, layer, now.value > then.value, change)):
            claims.append(effects)
    # The measure's other names and words of change steer the pick toward the sentence that says what moved.
    moved = RISE_WORDS if now.value >= then.value else FALL_WORDS
    context = " ".join([*layer.measures[mq.measure].synonyms, moved])
    passages, hits = await _explaining(principal, question, _doc_query(mq, layer, chosen), context, mq.period)
    claims += [Claim(p.text, (), (p.hit.chunk_id,)) for p in passages]
    if not passages:
        caveats.append(NO_DOCUMENT)
    draft = Draft(tuple(claims), tuple(caveats))
    evidence = Evidence(tuple(rows), hits, tuple(results), ())
    text = " ".join([*(c.text for c in claims), *caveats])
    sql, params = tuple(statement for statement, _ in ran), tuple(bound for _, bound in ran)
    return WhyResult("answer", text, draft, evidence, query=mq, sql=sql, params=params)
