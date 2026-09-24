import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

import sqlglot
import yaml
from sqlglot import exp

from app.config import ROOT
from app.semantic.query import COMPARES, GRAINS, MetricQuery, Period

LAYER_PATH = ROOT / "semantic" / "claims.yaml"

# The views the compiler may name, with their columns. The catalog test checks these against the database.
VIEWS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "sem.v_claims": frozenset(
            {"claim_id", "region", "state", "peril", "loss_date", "reported_date", "closed_date", "status"}
            | {"channel", "adjuster_id", "edition", "deductible", "reserve", "denial_reason", "paid_total"}
        ),
        "sem.v_payments_net": frozenset(
            {"payment_id", "claim_id", "region", "state", "peril", "paid_date", "amount", "kind"}
        ),
        "sem.v_premium": frozenset({"region", "month", "earned_premium", "exposure"}),
    }
)
DATE_COLUMNS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "sem.v_claims": ("loss_date", "reported_date", "closed_date"),
        "sem.v_payments_net": ("paid_date",),
        "sem.v_premium": ("month",),
    }
)
# A payment row reaches the columns of its claim through claim_id.
CLAIM_VIEW = "sem.v_claims"

Kind = Literal["count", "sum", "ratio"]
Format = Literal["integer", "currency", "percent"]
Target = tuple[str, ...]


class LayerError(ValueError):
    pass


@dataclass(frozen=True)
class Measure:
    name: str
    label: str
    kind: Kind
    format: Format
    sources: tuple[str, ...]
    dates: tuple[str, ...]
    sql: str | None
    numerator: str | None
    denominator: str | None
    where: str | None
    synonyms: tuple[str, ...]
    point_in_time: bool
    analyst: bool


@dataclass(frozen=True)
class Dimension:
    name: str
    column: str | None
    values: tuple[str, ...]
    synonyms: Mapping[str, str]
    analyst: bool

    @property
    def is_grain(self) -> bool:
        return self.column is None


@dataclass(frozen=True)
class Phrase:
    text: str
    target: Target
    case_sensitive: bool


@dataclass(frozen=True)
class Example:
    q: str
    query: MetricQuery


@dataclass(frozen=True)
class Layer:
    as_of: date
    coverage: Period
    measures: Mapping[str, Measure]
    dimensions: Mapping[str, Dimension]
    phrases: tuple[Phrase, ...]
    examples: tuple[Example, ...]

    def dimensions_for(self, measure: Measure, *, analyst: bool = False) -> tuple[str, ...]:
        """Dimensions a measure can be filtered or grouped by, in layer order."""
        if analyst and not measure.analyst:
            return ()
        reachable = frozenset.intersection(*(reach(source) for source in measure.sources))
        # A column the measure already pins in its own row filter can't be asked about again.
        fixed = measure.where or ""
        names = []
        for dim in self.dimensions.values():
            if analyst and not dim.analyst:
                continue
            if dim.is_grain:
                if not measure.point_in_time:
                    names.append(dim.name)
            elif dim.column in reachable and not re.search(rf"\b{dim.column}\b", fixed):
                names.append(dim.name)
        return tuple(names)


def reach(source: str) -> frozenset[str]:
    columns = VIEWS[source]
    return columns | VIEWS[CLAIM_VIEW] if "claim_id" in columns else columns


def _columns(fragment: str) -> set[str]:
    return {col.name for col in sqlglot.parse_one(fragment, read="postgres").find_all(exp.Column)}


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(str(v) for v in (value if isinstance(value, list) else [value]))


def _measure(name: str, spec: Mapping[str, Any]) -> Measure:
    sources = _as_tuple(spec.get("source"))
    unknown = [s for s in sources if s not in VIEWS]
    if not sources or unknown:
        raise LayerError(f"measure {name}: unknown source view {unknown or '(none)'}")
    point_in_time = bool(spec.get("point_in_time", False))
    dates: tuple[str, ...] = ()
    if not point_in_time:
        wanted = spec.get("date")
        if not wanted:
            raise LayerError(f"measure {name}: needs a date column or point_in_time: true")
        dates = tuple(_date_for(name, source, str(wanted)) for source in sources)
    kind = spec.get("kind")
    if kind not in ("count", "sum", "ratio"):
        raise LayerError(f"measure {name}: kind must be count, sum or ratio, not {kind!r}")
    measure = Measure(
        name=name,
        label=str(spec.get("label", name.replace("_", " "))),
        kind=cast(Kind, kind),
        format=cast(Format, spec.get("format")),
        sources=sources,
        dates=dates,
        sql=spec.get("sql"),
        numerator=spec.get("numerator"),
        denominator=spec.get("denominator"),
        where=spec.get("where"),
        synonyms=_as_tuple(spec.get("synonyms")),
        point_in_time=point_in_time,
        analyst=bool(spec.get("analyst", not point_in_time)),
    )
    if measure.format not in ("integer", "currency", "percent"):
        raise LayerError(f"measure {name}: unknown format {measure.format!r}")
    if kind == "ratio":
        required = [("numerator", measure.numerator, sources[0]), ("denominator", measure.denominator, sources[-1])]
    else:
        required = [("sql", measure.sql, sources[0])]
    for key, fragment, _ in required:
        if not fragment:
            raise LayerError(f"measure {name}: a {kind} measure needs {key}")
    for key, fragment, source in [*required, ("where", measure.where, sources[0])]:
        if fragment and (stray := _columns(fragment) - reach(source)):
            raise LayerError(f"measure {name}: {key} names {sorted(stray)}, which {source} doesn't have")
    return measure


def _date_for(name: str, source: str, wanted: str) -> str:
    if wanted in DATE_COLUMNS[source]:
        return wanted
    if len(DATE_COLUMNS[source]) == 1:
        return DATE_COLUMNS[source][0]
    raise LayerError(f"measure {name}: {source} has no date column {wanted}")


def _dimension(name: str, spec: Mapping[str, Any]) -> Dimension:
    if "grain" in spec:
        if spec["grain"] not in GRAINS or spec["grain"] != name:
            raise LayerError(f"dimension {name}: grain must be one of {GRAINS} and match its name")
        return Dimension(name, None, (), MappingProxyType({}), True)
    values = _as_tuple(spec.get("values"))
    synonyms = {str(k): str(v) for k, v in (spec.get("synonyms") or {}).items()}
    bad = sorted(v for v in synonyms.values() if v not in values)
    if bad:
        raise LayerError(f"dimension {name}: synonyms map to unknown values {bad}")
    column = str(spec.get("column", name))
    if not any(column in columns for columns in VIEWS.values()):
        raise LayerError(f"dimension {name}: no view has a column {column}")
    return Dimension(name, column, values, MappingProxyType(synonyms), bool(spec.get("analyst", True)))


def _is_code(value: str) -> bool:
    return len(value) <= 3 and value.isupper()


def _phrases(measures: Mapping[str, Measure], dimensions: Mapping[str, Dimension]) -> tuple[Phrase, ...]:
    entries: list[tuple[str, Target, bool]] = []
    for m in measures.values():
        entries += [(text, ("measure", m.name), False) for text in (*m.synonyms, m.label)]
    for d in dimensions.values():
        entries += [(v, ("value", d.name, v), _is_code(v)) for v in d.values]
        entries += [(text, ("value", d.name, v), False) for text, v in d.synonyms.items()]
    seen: dict[tuple[str, bool], Target] = {}
    for text, target, exact in entries:
        key = (text if exact else text.lower(), exact)
        if seen.setdefault(key, target) != target:
            raise LayerError(f"synonym {text!r} maps to both {seen[key]} and {target}")
    phrases = [Phrase(text, target, exact) for (text, exact), target in seen.items()]
    return tuple(sorted(phrases, key=lambda p: (-len(p.text), p.text)))


def _example(layer: Layer, raw: Mapping[str, Any]) -> Example:
    try:
        query = MetricQuery.from_spec(raw["query"])
    except (KeyError, ValueError) as exc:
        raise LayerError(f"example {raw.get('q')!r}: {exc}") from None
    measure = layer.measures.get(query.measure)
    problems = [] if measure else [f"unknown measure {query.measure}"]
    for dim, values in query.filters.items():
        known = layer.dimensions.get(dim)
        if known is None:
            problems.append(f"unknown dimension {dim}")
        elif stray := [v for v in values if v not in known.values]:
            problems.append(f"unknown {dim} values {stray}")
    problems += [f"unknown dimension {g}" for g in query.group_by if g not in layer.dimensions]
    if query.compare_to is not None and query.compare_to not in COMPARES:
        problems.append(f"unknown comparison {query.compare_to}")
    if problems:
        raise LayerError(f"example {raw['q']!r}: {'; '.join(problems)}")
    return Example(str(raw["q"]), query)


def parse_layer(doc: Mapping[str, Any]) -> Layer:
    coverage = doc["coverage"]
    measures = {name: _measure(name, spec) for name, spec in doc["measures"].items()}
    dimensions = {name: _dimension(name, spec or {}) for name, spec in doc["dimensions"].items()}
    layer = Layer(
        as_of=doc["as_of"],
        coverage=Period.between(coverage["start"], coverage["end"]),
        measures=MappingProxyType(measures),
        dimensions=MappingProxyType(dimensions),
        phrases=_phrases(measures, dimensions),
        examples=(),
    )
    examples = tuple(_example(layer, raw) for raw in doc.get("examples") or ())
    return Layer(layer.as_of, layer.coverage, layer.measures, layer.dimensions, layer.phrases, examples)


def load_layer(path: Path = LAYER_PATH) -> Layer:
    with path.open() as fh:
        return parse_layer(yaml.safe_load(fh))


@cache
def default_layer() -> Layer:
    return load_layer()
