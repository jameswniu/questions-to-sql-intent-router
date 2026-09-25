# Defence in depth ahead of the container, not the boundary itself: it turns the obvious escape
# attempts into a legible refusal before a container is spent on them. Standard library only,
# because the sandboxd image imports this same file.
from __future__ import annotations

import ast
from dataclasses import dataclass

BANNED_NAMES = frozenset(
    {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "input",
        "breakpoint",
    }
)
BANNED_NODES: dict[type[ast.stmt], str] = {
    ast.Import: "import",
    ast.ImportFrom: "import",
    ast.Global: "global",
    ast.Nonlocal: "nonlocal",
    ast.ClassDef: "class definition",
    ast.AsyncFunctionDef: "async function",
}
MAX_SOURCE_BYTES = 64 * 1024
# The names sandbox/runner.py binds for adapted code, as this refusal and the analysis prompt list them. A test holds
# the list to the runner's own.
PROVIDED = ("pd", "np", "math", "statistics", "Fraction")
PROVIDED_TEXT = f"{', '.join(PROVIDED[:-1])} and {PROVIDED[-1]}"


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None = None


def _is_constant(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_constant(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _is_constant(k) for k in node.keys) and all(_is_constant(v) for v in node.values)
    if isinstance(node, ast.UnaryOp):
        return _is_constant(node.operand)
    if isinstance(node, ast.BinOp):
        return _is_constant(node.left) and _is_constant(node.right)
    return False


def _check_top_level(tree: ast.Module) -> str | None:
    runs = 0
    for i, stmt in enumerate(tree.body):
        if isinstance(stmt, ast.FunctionDef):
            if stmt.decorator_list:
                return f"line {stmt.lineno}: decorators are not allowed"
            if stmt.name == "run":
                runs += 1
                a = stmt.args
                if [p.arg for p in a.args] != ["df", "params"] or a.posonlyargs or a.kwonlyargs:
                    return f"line {stmt.lineno}: run must take exactly (df, params)"
                if a.vararg or a.kwarg or a.defaults:
                    return f"line {stmt.lineno}: run must take exactly (df, params)"
        elif isinstance(stmt, ast.Assign):
            if not all(isinstance(t, ast.Name) for t in stmt.targets) or not _is_constant(stmt.value):
                return f"line {stmt.lineno}: top-level assignments must bind a name to a constant"
        elif isinstance(stmt, ast.AnnAssign):
            if not isinstance(stmt.target, ast.Name) or stmt.value is None or not _is_constant(stmt.value):
                return f"line {stmt.lineno}: top-level assignments must bind a name to a constant"
        elif i == 0 and isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        elif not any(isinstance(stmt, banned) for banned in BANNED_NODES):
            return f"line {stmt.lineno}: only function definitions and constants may appear at the top level"
    if runs != 1:
        return "the code must define exactly one top-level function run(df, params)"
    return None


def check(code: str) -> Verdict:
    if len(code.encode()) > MAX_SOURCE_BYTES:
        return Verdict(False, f"source is larger than {MAX_SOURCE_BYTES} bytes")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        return Verdict(False, f"syntax error on line {exc.lineno}: {exc.msg}")

    for node in ast.walk(tree):
        for banned, label in BANNED_NODES.items():
            if isinstance(node, banned):
                return Verdict(False, f"line {node.lineno}: {label} is not allowed; {PROVIDED_TEXT} are provided")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            return Verdict(False, f"line {node.lineno}: attribute {node.attr!r} starts with an underscore")
        if isinstance(node, ast.Name) and (node.id in BANNED_NAMES or node.id.startswith("__")):
            return Verdict(False, f"line {node.lineno}: name {node.id!r} is not allowed")

    reason = _check_top_level(tree)
    return Verdict(False, reason) if reason else Verdict(True)
