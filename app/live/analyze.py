# The analysis sub-agent. It must never import app.db: it gets rows the SQL sub-agent already read, and the only
# place it sends them is the sandbox.
import ast
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import pydantic

from app.live.datasets import Dataset
from app.live.errors import Invalid, SandboxError
from app.live.prompts import main_system
from app.llm.client import LLM, ask, output_for, parse
from app.llm.facts import Fact, Token
from app.llm.request import Message, Request
from app.sandbox import codecheck
from app.sandbox.client import JobResult, Sandbox, Table
from app.semantic.layer import Layer

TEMPLATE = "why.analyze.v1"
# The golden templates are read as text, never imported: they need pandas, which only the sandbox image has.
GOLDEN_SOURCE = (Path(codecheck.__file__).parent / "templates.py").read_text()


def _names(source: str) -> tuple[str, ...]:
    """The template names, the keys of the TEMPLATES table at the end of the golden templates' file."""
    for node in ast.parse(source).body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id == "TEMPLATES" and isinstance(node.value, ast.Dict):
            return tuple(str(key.value) for key in node.value.keys if isinstance(key, ast.Constant))
    raise RuntimeError("the golden templates' file has no TEMPLATES table")


TEMPLATES = _names(GOLDEN_SOURCE)
CHANGE_TEMPLATES = ("yoy", "decompose")
# Money comes back in dollars from sums of cents, so an adaptation may round a split a cent differently. A share
# or a rate, under one, gets no such slack. Past that, only floating point noise is allowed.
CENT = 0.01
FRACTION_TOLERANCE = 1e-6
RELATIVE_TOLERANCE = 1e-9
INSTRUCTIONS = f"""\
You adapt one of the golden analysis templates below into standalone code for a sandbox.

Write Python that defines exactly one top-level function run(df, params) and returns a dict with the same keys and \
the same numbers as the named template would for this df and params. The sandbox provides pd, np, math and \
statistics as names; it refuses imports, classes, async functions, decorators, attributes that start with an \
underscore, and names such as open, eval, exec and getattr. Helper functions and constants may sit at the top level. \
Reply with the code only, in the code field.

The golden templates:

{GOLDEN_SOURCE}"""


class Runner(Protocol):
    """What analyze needs from sandboxd's client, so a test can stand in for the daemon."""

    async def run(
        self, principal_role: str, *, template: str | None, code: str | None, params: dict[str, Any], table: Table
    ) -> JobResult: ...


class Code(pydantic.BaseModel):
    code: str


@dataclass(frozen=True)
class Analysis:
    handle: str
    template: str
    dataset: str
    params: dict[str, Any]
    # The golden template's output on the same rows, which is what the evidence holds.
    result: dict[str, Any]


@dataclass(frozen=True)
class Analyzed:
    status: Literal["ok", "not_supported"]
    analysis: Analysis | None = None


def job(dataset: Dataset, template: str, layer: Layer) -> tuple[Table, dict[str, Any]] | None:
    """The table and params a template runs on, or None when the template doesn't fit what the dataset holds."""
    mq = dataset.query
    if layer.measures[mq.measure].kind == "ratio":
        return None
    compare = mq.compare_to is not None
    if template in CHANGE_TEMPLATES:
        if not compare or mq.grain is not None or len(mq.group_by) > 1:
            return None
        group = mq.group_by[0] if mq.group_by else None
        keys = ["period", *mq.group_by]
        params: dict[str, Any] = {"value": "value", "period": "period", "base": "prior", "current": "current"}
        params["group"] = group
    else:
        if compare or mq.grain is None:
            return None
        keys = [*mq.group_by, mq.grain]
        params = {"value": "value", "period": mq.grain}
    rows = []
    for record in dataset.records():
        if dataset.kind == "agg":
            if record["suppressed"] or record["num"] is None:
                continue
            record = {**(record["grp"] or {}), "period": record.get("period"), "value": record["num"]}
        rows.append([*(record[key] for key in keys), record["value"]])
    return Table([*keys, "value"], rows), params


def request(model: str, template: str, table: Table, params: Mapping[str, Any]) -> Request:
    ask_text = (
        f"Template: {template}\nColumns: {', '.join(table.columns)}\nParams: {json.dumps(params, sort_keys=True)}"
    )
    return Request(
        TEMPLATE,
        model,
        main_system(INSTRUCTIONS),
        (Message("user", (ask_text,)),),
        max_tokens=4096,
        output=output_for(Code),
        cache=True,
        effort="low",
        timeout_s=30.0,
    )


def agrees(adapted: Any, golden: Any) -> bool:
    """Whether the adapted output has every value the golden one has, numbers within rounding."""
    if isinstance(golden, Mapping):
        return isinstance(adapted, Mapping) and all(k in adapted and agrees(adapted[k], v) for k, v in golden.items())
    if isinstance(golden, list):
        return isinstance(adapted, list) and len(adapted) == len(golden) and all(map(agrees, adapted, golden))
    if isinstance(golden, int | float) and not isinstance(golden, bool):
        if not isinstance(adapted, int | float) or isinstance(adapted, bool):
            return False
        absolute = CENT if max(abs(adapted), abs(golden)) >= 1 else FRACTION_TOLERANCE
        return math.isclose(adapted, golden, rel_tol=RELATIVE_TOLERANCE, abs_tol=absolute)
    return bool(adapted == golden)


def _ran(result: JobResult, what: str) -> dict[str, Any]:
    if not result.ok or result.result is None:
        raise SandboxError(f"the {what} run failed: {result.killed or result.error or result.status}")
    return result.result


async def analyze(
    llm: LLM,
    role: str,
    dataset: Dataset,
    template: str,
    handle: str,
    layer: Layer,
    *,
    sandbox: Runner | None = None,
) -> Analyzed:
    """The analysis sub-agent. A model adapts the golden template into code, the code has to pass the sandbox's own
    check, and it runs in sandboxd on the dataset's rows. The golden template runs on the same rows, and the two
    have to agree, so a number the model's code got wrong never reaches the answer."""
    fitted = job(dataset, template, layer)
    if fitted is None:
        return Analyzed("not_supported")
    table, params = fitted
    code = parse(await ask(llm, request(llm.main_model, template, table, params)), Code).code
    verdict = codecheck.check(code)
    if not verdict.ok:
        raise Invalid(f"the adapted code was refused: {verdict.reason}")
    box: Runner = sandbox or Sandbox()
    adapted = _ran(await box.run(role, template=None, code=code, params=params, table=table), "adapted")
    golden = _ran(await box.run(role, template=template, code=None, params=params, table=table), "golden")
    if not agrees(adapted, golden):
        raise Invalid("the adapted code disagreed with the golden template")
    return Analyzed("ok", Analysis(handle, template, dataset.handle, params, golden))


def _fact(value: Any, vocabulary: frozenset[str]) -> Fact:
    if isinstance(value, Mapping):
        return {str(key): _fact(inner, vocabulary) for key, inner in value.items()}
    if isinstance(value, list):
        return [_fact(inner, vocabulary) for inner in value]
    if isinstance(value, str):
        # A group key is a value of the layer; anything else here is a period or a date, which is one token too.
        return Token.word(value, vocabulary) if value in vocabulary else Token(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    raise Invalid(f"the analysis returned a {type(value).__name__}")


def summary(analysis: Analysis, layer: Layer) -> dict[str, Fact]:
    """What the orchestrator learns from analyze: the handle, the template and the numeric result."""
    vocabulary = frozenset(v for dim in layer.dimensions.values() for v in dim.values)
    return {
        "status": Token("ok"),
        "analysis": Token.handle(analysis.handle),
        "template": Token.word(analysis.template, TEMPLATES),
        "result": _fact(analysis.result, vocabulary),
    }
