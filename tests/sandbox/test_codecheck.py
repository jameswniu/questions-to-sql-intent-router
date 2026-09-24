from __future__ import annotations

import pytest

from app.sandbox.codecheck import MAX_SOURCE_BYTES, check

ALLOWED = {
    "plain transform": """
def run(df, params):
    return {"total": float(df["paid"].sum())}
""",
    "helpers and constants": '''
"""Severity by peril."""
THRESHOLD = 2.5
PERILS = ("water", "wind")
LIMIT: int = 10 * 1000

def by_peril(df):
    return df.groupby("peril")["paid"].mean()

def run(df, params):
    s = by_peril(df)
    return {k: float(v) for k, v in s.items() if v > THRESHOLD}
''',
    "an endless loop, which the wall clock ends": """
def run(df, params):
    while True:
        pass
""",
    "lambdas, comprehensions and the provided modules": """
def run(df, params):
    xs = sorted(df["paid"], key=lambda v: -v)
    return {"top": xs[:3], "sd": statistics.pstdev(xs), "e": math.e, "n": int(np.size(xs))}
""",
}

REJECTED = {
    "import": ("import os\ndef run(df, params):\n    return {}", "import"),
    "import inside run": ("def run(df, params):\n    import socket\n    return {}", "import"),
    "from import": ("from os import system\ndef run(df, params):\n    return {}", "import"),
    "open": ("def run(df, params):\n    return {'x': open('/etc/passwd').read()}", "'open'"),
    "eval": ("def run(df, params):\n    return eval('{}')", "'eval'"),
    "exec": ("def run(df, params):\n    exec('x = 1')\n    return {}", "'exec'"),
    "__import__": ("def run(df, params):\n    return {'m': str(__import__('os'))}", "'__import__'"),
    "dunder attribute": ("def run(df, params):\n    return {'m': str(df.__class__)}", "'__class__'"),
    "private attribute": ("def run(df, params):\n    return {'m': str(pd._libs)}", "'_libs'"),
    "getattr": ("def run(df, params):\n    return getattr(df, 'x')", "'getattr'"),
    "getattr passed around": ("def run(df, params):\n    f = getattr\n    return {}", "'getattr'"),
    "globals": ("def run(df, params):\n    return globals()", "'globals'"),
    "__builtins__": ("def run(df, params):\n    return {'b': str(__builtins__)}", "'__builtins__'"),
    "global statement": ("X = 1\ndef run(df, params):\n    global X\n    return {}", "global"),
    "nonlocal statement": (
        "def run(df, params):\n    n = 0\n    def f():\n        nonlocal n\n    return {}",
        "nonlocal",
    ),
    "class": ("class A:\n    pass\ndef run(df, params):\n    return {}", "class"),
    "no run": ("def main(df, params):\n    return {}", "exactly one"),
    "two runs": ("def run(df, params):\n    return {}\ndef run(df, params):\n    return {}", "exactly one"),
    "wrong signature": ("def run(df):\n    return {}", "(df, params)"),
    "extra keyword": ("def run(df, params, *, x=1):\n    return {}", "(df, params)"),
    "top-level call": ("print('hi')\ndef run(df, params):\n    return {}", "top level"),
    "top-level computed value": ("X = pd.DataFrame()\ndef run(df, params):\n    return {}", "constant"),
    "decorator": ("@staticmethod\ndef run(df, params):\n    return {}", "decorators"),
    "async": ("async def run(df, params):\n    return {}", "async"),
    "syntax error": ("def run(df, params)\n    return {}", "syntax error"),
}


@pytest.mark.parametrize("code", ALLOWED.values(), ids=ALLOWED.keys())
def test_codecheck_allows_plain_data_transforms(code: str) -> None:
    verdict = check(code)
    assert verdict.ok, verdict.reason


@pytest.mark.parametrize(("code", "why"), REJECTED.values(), ids=REJECTED.keys())
def test_codecheck_rejects_escape_hatches_with_a_legible_reason(code: str, why: str) -> None:
    verdict = check(code)
    assert not verdict.ok
    assert verdict.reason and why in verdict.reason


def test_codecheck_rejects_source_over_the_size_limit() -> None:
    code = "def run(df, params):\n    return {}\n" + "#" * MAX_SOURCE_BYTES
    assert not check(code).ok


def test_codecheck_is_not_a_boundary_it_passes_code_that_reaches_the_os_through_pandas() -> None:
    # pandas modules keep their own references to os, sys and subprocess, under names with no
    # underscore. This is why the container, not this check, is the security boundary.
    code = """
def run(df, params):
    subprocess = pd.compat.sys.modules["subprocess"]
    return {"id": subprocess.run(["id"], capture_output=True, text=True).stdout, "os": str(pd.io.common.os)}
"""
    assert check(code).ok
