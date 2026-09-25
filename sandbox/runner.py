# Runs inside the throwaway container: one job on stdin, one JSON object on stdout.
# The container is the security boundary. The restricted globals below only make a failure
# read as "open is not defined" instead of a stack trace from somewhere deep in the OS.
from __future__ import annotations

import builtins
import contextlib
import datetime
import json
import math
import os
import statistics
import sys
from decimal import Decimal
from fractions import Fraction
from typing import Any

# -I leaves the script's own directory off sys.path; /runner is root-owned on a read-only root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from templates import TEMPLATES  # type: ignore[import-not-found]  # noqa: E402 - copied beside this file in the image

SAFE_BUILTINS = {
    name: getattr(builtins, name)
    for name in (
        "abs", "all", "any", "bool", "callable", "chr", "dict", "divmod", "enumerate", "filter", "float",
        "format", "frozenset", "hash", "int", "isinstance", "issubclass", "iter", "len", "list", "map", "max",
        "min", "next", "ord", "pow", "print", "range", "repr", "reversed", "round", "set", "slice", "sorted",
        "str", "sum", "tuple", "zip",
        "ArithmeticError", "AssertionError", "Exception", "IndexError", "KeyError", "LookupError",
        "OverflowError", "RuntimeError", "StopIteration", "TypeError", "ValueError", "ZeroDivisionError",
    )
}  # fmt: skip
# The names adapted code runs with, beside the builtins above. Code can't import, so these have to include every
# name the golden templates use as they run, or a template can't be copied faithfully: decompose's exact split needs
# Fraction. codecheck's refusal and the analysis prompt list the same names, and a test holds them together.
PROVIDED: dict[str, Any] = {"pd": pd, "np": np, "math": math, "statistics": statistics, "Fraction": Fraction}
MAX_ERROR_CHARS = 2000


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (pd.Series, pd.Index, np.ndarray)):
        return [_jsonable(v) for v in value.tolist()]
    if isinstance(value, pd.DataFrame):
        return {"columns": [str(c) for c in value.columns], "rows": _jsonable(value.to_numpy().tolist())}
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _load_code(code: str) -> Any:
    scope: dict[str, Any] = {"__builtins__": SAFE_BUILTINS, "__name__": "job", **PROVIDED}
    exec(compile(code, "<job>", "exec"), scope)
    run = scope.get("run")
    if not callable(run):
        raise ValueError("the code does not define run(df, params)")
    return run


def execute(job: dict[str, Any]) -> dict[str, Any]:
    table = job.get("table") or {}
    df = pd.DataFrame(table.get("rows") or [], columns=table.get("columns") or [])
    params = job.get("params") or {}
    if job.get("template"):
        fn = TEMPLATES.get(job["template"])
        if fn is None:
            raise ValueError(f"unknown template {job['template']!r}")
    elif job.get("code"):
        fn = _load_code(job["code"])
    else:
        raise ValueError("the job names neither a template nor code")
    result = fn(df, params)
    if not isinstance(result, dict):
        raise TypeError(f"run must return a dict, got {type(result).__name__}")
    reply: dict[str, Any] = _jsonable(result)
    return reply


def main() -> int:
    out = sys.stdout
    try:
        job = json.loads(sys.stdin.buffer.read())
        # Anything the job prints goes to stderr so stdout carries exactly one JSON object.
        with contextlib.redirect_stdout(sys.stderr):
            reply: dict[str, Any] = {"ok": True, "result": execute(job)}
    except BaseException as exc:  # noqa: BLE001 - every failure must come back as one JSON object
        reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]}
    out.write(json.dumps(reply, allow_nan=False, separators=(",", ":")))
    out.write("\n")
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
