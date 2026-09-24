from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from app.answer import drivers as dv
from app.answer.format import NumberRef, change_refs, ref
from app.answer.types import Claim
from app.live.analyze import CHANGE_TEMPLATES, Analysis
from app.live.datasets import Dataset
from app.sandbox.client import Table
from app.semantic.describe import is_plural, state_names, subject
from app.semantic.layer import Layer
from app.semantic.query import MetricQuery

# The figure sentences of a live answer. They are written here, the way the no-key workflow writes them, and never
# by a model: a figure a model placed in a sentence can be the right number beside the wrong period or group, and
# the verifier, which traces each figure to the evidence, can't tell.


@dataclass(frozen=True)
class Headline:
    claim: Claim
    # The change and percent change refs, which a sentence naming the rise or fall carries too.
    change: tuple[NumberRef, ...]
    rising: bool
    # The change as it was queried and read. Its dataset's rows come first in the evidence, so the two sides' row
    # numbers are evidence rows too.
    query: MetricQuery
    kind: Literal["rows", "agg"]
    current: dv.Side
    prior: dv.Side


def headline(dataset: Dataset, layer: Layer) -> Headline | None:
    """The change the question asked about: this period, the one before, and the move, or None when there is no pair
    of figures to compare. The dataset's rows come first in the evidence, so each figure's derivation names its own
    row."""
    mq, measure = dataset.query, layer.measures[dataset.query.measure]
    ratio, agg = measure.kind == "ratio", dataset.kind == "agg"
    sides = dv.sides(dv.Fetched(dataset.columns, dataset.rows, dataset.sql), ratio)
    now, then = sides.get("current"), sides.get("prior")
    if mq.period is None or now is None or then is None or now.value is None or then.value is None:
        return None
    column, source = ("num", "num / den" if ratio else "num") if agg else ("value", "value")
    if ratio and not agg:
        source = "numerator / denominator"
    current = ref(now.value, measure.format, column, now.row, source)
    prior = ref(then.value, measure.format, column, then.row, source)
    changes = change_refs(now.value, then.value, measure.format, (now.row, then.row), source)
    moved = "unchanged"
    if now.value != then.value:
        pct = f" ({changes[1].display})" if len(changes) > 1 else ""
        moved = f"{'up' if now.value > then.value else 'down'} {changes[0].display}{pct}"
    verb = "were" if is_plural(measure) else "was"
    text = (
        f"{subject(mq, layer)} {verb} {current.display} in {mq.period.label}"
        f" and {prior.display} in {mq.period.prior().label}, {moved}."
    )
    claim = Claim(text, (current, prior, *changes), ())
    return Headline(claim, changes, now.value > then.value, mq, dataset.kind, now, then)


@dataclass(frozen=True)
class Split:
    """An analysis that splits the headline's own change by one dimension, and the dataset it ran on."""

    dim: str
    analysis: Analysis
    dataset: Dataset


def _shape(mq: MetricQuery) -> tuple[object, ...]:
    filters = tuple(sorted((dim, tuple(sorted(values))) for dim, values in mq.filters.items() if values))
    return mq.measure, filters, mq.group_by, mq.period, mq.compare_to, mq.grain, mq.limit


def _dimension(analysis: Analysis, dataset: Dataset, change: Headline, free: Sequence[str]) -> str | None:
    query = dataset.query
    if analysis.template not in CHANGE_TEMPLATES or dataset.kind != change.kind:
        return None
    if len(query.group_by) != 1 or query.group_by[0] not in free:
        return None
    return query.group_by[0] if _shape(query) == _shape(replace(change.query, group_by=query.group_by)) else None


def splits(
    analyses: Sequence[Analysis], datasets: Mapping[str, Dataset], change: Headline, free: Sequence[str]
) -> list[Split]:
    """The analyses that split the headline's own change by one of its free dimensions, the first named for each.
    An analysis of anything else, another measure, filter or period, a limit, or a dimension the change can't be
    split by, can't say which groups account for the change, so the answer doesn't rest on it."""
    found: dict[str, Split] = {}
    for analysis in analyses:
        dataset = datasets[analysis.dataset]
        dim = _dimension(analysis, dataset, change, free)
        if dim is not None and dim not in found:
            found[dim] = Split(dim, analysis, dataset)
    return list(found.values())


def _at(dataset: Dataset, dim: str) -> dict[tuple[str, str], int]:
    """Where each group's row for each period sits in the dataset. An aggregate's suppressed cells never reach the
    sandbox, so, as in the no-key workflow, a group missing from a period counts as zero there."""
    at: dict[tuple[str, str], int] = {}
    for index, record in enumerate(dataset.records()):
        if dataset.kind == "agg":
            if record["suppressed"] or record["num"] is None:
                continue
            key = (record["grp"] or {}).get(dim)
        else:
            key = record[dim]
        at[(str(record["period"]), str(key))] = index
    return at


def _groups(split: Split, offset: int, change: Headline) -> list[dv.Group]:
    """Each group's part of the headline's change and its share of it, worked out as the no-key workflow works them
    out: over the headline's change, never the analysis's own total, and written as arithmetic on the evidence rows,
    which the verifier recomputes."""
    part = "delta_total" if split.analysis.template == "decompose" else "change"
    groups = [
        {"group": group["group"], "delta_total": group[part]}
        for group in split.analysis.result.get("groups") or []
        if group.get(part) is not None
    ]
    # contributions reads only the dimension and where each group's rows sit. analyze runs no ratio measure, so a
    # group's part of the change is its own change.
    positions = dv.Split(split.dim, Table([], []), [], [], _at(split.dataset, split.dim))
    return dv.contributions(
        {"groups": groups},
        positions,
        offset,
        ratio=False,
        agg=change.kind == "agg",
        current=change.current,
        prior=change.prior,
    )


def _label(group: dv.Group) -> str:
    if group.dim == "state":
        return state_names().get(group.key, group.key)
    return f"the {group.key}" if group.dim == "region" else f"{group.key} claims"


def drivers(found: Sequence[Split], offsets: Mapping[str, int], change: Headline) -> Claim | None:
    """Which groups account for the change, picked and worded as the no-key workflow picks and words them. offsets
    says where each split's dataset starts in the evidence rows."""
    chosen = dv.pick_drivers([_groups(split, offsets[split.dataset.handle], change) for split in found])
    if not chosen:
        return None
    direction = "rise" if change.rising else "fall"
    first = chosen[0]
    refs = [group.ref for top in chosen for group in top]
    lead = _label(first[0])
    verb = "account" if lead.endswith(" claims") else "accounts"
    text = f"{lead[0].upper()}{lead[1:]} {verb} for {refs[0].display} of the {direction}"
    if len(refs) > 1:
        joiner = " and" if len(first) == 2 else ", and"
        text += f"{joiner} {_label(chosen[-1][-1])} for {refs[1].display}"
    return Claim(f"{text}.", (*refs, *change.change), ())
