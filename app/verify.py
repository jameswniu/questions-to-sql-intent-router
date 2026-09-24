import ast
import re
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import cache
from typing import Any, Literal

from app.answer.figures import FIGURE, NOT_QUANTITIES, Form, Written, figures
from app.answer.format import NumberRef
from app.answer.types import Answer, Claim, Draft, Evidence
from app.identity import Principal
from app.semantic.describe import group_label, state_names
from app.semantic.layer import default_layer
from app.semantic.query import MONTHS, month_end
from app.sources.documents import Hit


def _half_shown(written: Written) -> Decimal:
    return Decimal(5).scaleb(-(written.places + 1)) * written.scale


@dataclass(frozen=True)
class Rule:
    example: str
    says: str
    tolerance: Callable[[Written], Decimal]
    # The cited value is compared after each of these scalings, so 18.8% can cite 0.188 or 18.8.
    readings: tuple[Decimal, ...] = (Decimal(1),)


# How close a written figure must be to the value it cites, and how close a change must come to the multiple a word
# such as "doubled" states. Every tolerance the verifier grants is here.
NORMALIZATION: dict[Form | Literal["multiple"], Rule] = {
    "cents": Rule("$4,108,452.79", "within a cent", lambda w: Decimal("0.01")),
    "dollars": Rule("$4,108,453", "within the whole-dollar rounding, 50 cents", lambda w: Decimal("0.5")),
    "scaled": Rule(
        "$4.1 million, 4.1M, $412K",
        "within the display rounding at that scale: $4.1 million covers $4.05 million to $4.15 million",
        _half_shown,
    ),
    "percent": Rule(
        "18.8%, 16.3 points",
        "within half the last shown digit in points, 0.05 at one decimal; 12.5% matches 0.125 and 12.5",
        _half_shown,
        (Decimal(100), Decimal(1)),
    ),
    "decimal": Rule("0.81", "within half the last shown digit", _half_shown),
    "integer": Rule("1,001", "exact", lambda w: Decimal(0)),
    "multiple": Rule(
        "doubled, tripled, halved",
        "within 5% of the multiple: doubled covers 1.9 to 2.1 times the earlier value, a change of +90% to +110%",
        lambda w: w.value / 20,
    ),
}

# Comparative words, matched whole in any case: each must be borne out by a number the claim traces, or it is cut.
RISING = ("rose", "rise", "risen", "increased", "increase", "grew", "growth", "up", "higher")
FALLING = ("fell", "fall", "fallen", "decreased", "decrease", "declined", "decline", "dropped", "drop", "down", "lower")
HIGHEST = ("highest", "largest", "most", "biggest", "top", "max")
LOWEST = ("lowest", "smallest", "least", "fewest", "min")
MULTIPLES = {"doubled": Decimal(2), "tripled": Decimal(3), "halved": Decimal("0.5")}
# Phrases where a listed word neither moves nor ranks anything, as the word before it and the word.
NOT_COMPARING = frozenset(
    {("add", "up"), ("adds", "up"), ("added", "up"), ("made", "up"), ("make", "up"), ("makes", "up")}
    | {("at", "least"), ("at", "most")}
)
COMPARATIVE = re.compile(rf"\b(?:{'|'.join((*RISING, *FALLING, *HIGHEST, *LOWEST, *MULTIPLES))})\b", re.IGNORECASE)
# The one literal a derivation may hold besides row indexes: 100, a factor or divisor that writes a share as a percent.
SCALES = frozenset({Decimal(100)})
NOT_QUANTITY_FIELDS = frozenset({"claim_number", "date", "vendor"})
EXACT = Decimal("1e-9")
PAIR_LIMIT = 400


def matches(written: Written, ref: NumberRef) -> bool:
    if written.negative and ref.value >= 0:
        return False
    rule = NORMALIZATION[written.form]
    return any(abs(abs(ref.value) * k - written.value) <= rule.tolerance(written) for k in rule.readings)


def _finite(value: Any) -> Iterator[Decimal]:
    if isinstance(value, int | float | Decimal) and not isinstance(value, bool):
        number = Decimal(str(value))
        if number.is_finite():
            yield number


def _leaves(value: Any, key: str = "") -> Iterator[Decimal]:
    if key == "id" or key.endswith(("_id", "_number")) or key in NOT_QUANTITY_FIELDS:
        return
    if isinstance(value, Mapping):
        for name, inner in value.items():
            yield from _leaves(inner, str(name))
    elif isinstance(value, list | tuple):
        for inner in value:
            yield from _leaves(inner, key)
    else:
        yield from _finite(value)


def _scan_value(field: Mapping[str, Any]) -> Iterator[Decimal]:
    raw = field.get("value")
    if field.get("field") in NOT_QUANTITY_FIELDS or raw is None:
        return
    if isinstance(raw, str):
        try:
            raw = Decimal(raw.replace("$", "").replace(",", "").strip())
        except InvalidOperation:
            return
    yield from _finite(raw)


def evidence_values(evidence: Evidence) -> tuple[Decimal, ...]:
    """Every number the evidence holds: row cells, sandbox outputs at any depth, and scan field values."""
    values = [*_leaves(list(evidence.rows)), *_leaves(list(evidence.sandbox))]
    for field in evidence.scan_fields:
        values.extend(_scan_value(field))
    return tuple(values)


class _Unresolved(Exception):
    pass


def _cell(rows: Sequence[Mapping[str, Any]], row: int | None, name: str) -> Decimal:
    if row is None or not 0 <= row < len(rows):
        raise _Unresolved
    for number in _finite(rows[row].get(name)):
        return number
    raise _Unresolved


def _evaluate(node: ast.expr, rows: Sequence[Mapping[str, Any]], row: int | None) -> Decimal:
    """Recomputes a recorded derivation such as "(num / den)[0] - (num / den)[1]" from the result rows."""
    match node:
        case ast.Constant(value=int() | float() as number) if not isinstance(number, bool):
            return Decimal(str(number))
        case ast.Name(id=name):
            return _cell(rows, row, name)
        case ast.Subscript(value=inner, slice=ast.Constant(value=int() as index)) if not isinstance(index, bool):
            return _evaluate(inner, rows, index)
        case ast.UnaryOp(op=ast.USub(), operand=operand):
            return -_evaluate(operand, rows, row)
        case ast.BinOp(left=left, op=ast.Sub() | ast.Add() | ast.Mult() | ast.Div() as op, right=right):
            a, b = _evaluate(left, rows, row), _evaluate(right, rows, row)
            if isinstance(op, ast.Div):
                if b == 0:
                    raise _Unresolved
                return a / b
            return a - b if isinstance(op, ast.Sub) else a + b if isinstance(op, ast.Add) else a * b
    raise _Unresolved


def _same(a: Decimal, b: Decimal) -> bool:
    a, b = abs(a), abs(b)
    return abs(a - b) <= EXACT * max(Decimal(1), a, b)


def _follows(value: Decimal, pool: tuple[Decimal, ...]) -> bool:
    """In the evidence, or the difference, ratio or percent change of two values in it."""
    if any(_same(value, x) for x in pool):
        return True
    distinct = [float(x) for x in set(pool)]
    if len(distinct) > PAIR_LIMIT:
        return False
    target = abs(float(value))
    for i, a in enumerate(distinct):
        for j, b in enumerate(distinct):
            outcomes = () if i == j else (a - b,) if b == 0 else (a - b, a / b, (a - b) / abs(b))
            if any(abs(abs(x) - target) <= 1e-9 * max(1.0, target) for x in outcomes):
                return True
    return False


def _loose_literals(node: ast.expr, scale: bool = False) -> Iterator[ast.expr]:
    """Literals that could stand in for the evidence: all but row indexes, and 100 as a factor or divisor."""
    match node:
        case ast.Subscript(value=inner):
            yield from _loose_literals(inner)
        case ast.Constant(value=int() | float() as number) if (
            scale and not isinstance(number, bool) and Decimal(str(number)) in SCALES
        ):
            return
        case ast.Constant():
            yield node
        case ast.BinOp(left=left, op=ast.Mult(), right=right):
            yield from _loose_literals(left, True)
            yield from _loose_literals(right, True)
        case ast.BinOp(left=left, op=ast.Div(), right=right):
            yield from _loose_literals(left)
            yield from _loose_literals(right, True)
        case _:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.expr):
                    yield from _loose_literals(child)


def _unanchored(node: ast.expr) -> str | None:
    """Why a derivation doesn't rest on the evidence: it names no cell, or a literal could supply its value."""
    if not any(isinstance(n, ast.Name) for n in ast.walk(node)):
        return "which names no cell of the evidence"
    if (literal := next(_loose_literals(node), None)) is not None:
        return f"which leans on the literal {ast.unparse(literal)} rather than the evidence"
    return None


def _grounding(ref: NumberRef, rows: Sequence[Mapping[str, Any]], pool: tuple[Decimal, ...]) -> str | None:
    """Why the ref's value can't be trusted, or None when the evidence holds it or it follows from the evidence."""
    if not ref.derivation.strip():
        return f"{ref.display}, which has no recorded derivation"
    node = _parsed(ref.derivation)
    if node is not None and (unanchored := _unanchored(node)):
        return f"{ref.display}, recorded as {ref.derivation}, {unanchored}"
    try:
        if node is None:
            raise _Unresolved
        computed = _evaluate(node, rows, ref.row_index)
    except (_Unresolved, ArithmeticError):
        # A derivation over sandbox outputs or scan fields names nothing in the rows; search the whole evidence.
        return (
            None
            if _follows(ref.value, pool)
            else f"{ref.display}, which isn't in the evidence and doesn't follow from it"
        )
    if _same(computed, ref.value):
        return None
    return f"{ref.display}, recorded as {ref.derivation}, which comes to {computed:,.4f}"


def _closest(written: Written, refs: tuple[NumberRef, ...]) -> str:
    if not refs:
        return ""
    readings = NORMALIZATION[written.form].readings
    nearest = min(refs, key=lambda ref: min(abs(abs(ref.value) * k - written.value) for k in readings))
    return f" (closest is {nearest.display})"


def _quotes(written: Written, passage: Written) -> bool:
    same_unit = (written.form == "percent") == (passage.form == "percent") and written.negative == passage.negative
    return same_unit and abs(passage.value - written.value) <= NORMALIZATION[written.form].tolerance(written)


def _figure_reasons(
    claim: Claim, rows: Sequence[Mapping[str, Any]], pool: tuple[Decimal, ...], passages: tuple[Written, ...]
) -> list[str]:
    """A figure holds when a matching ref is grounded, or when a retrieved passage the claim cites writes it too:
    a quoted "within 30 days" or "2% deductible" traces to the document it came from, not to a result row."""
    grounding = {ref: _grounding(ref, rows, pool) for ref in claim.numbers}
    reasons = []
    for written in figures(claim.text):
        cited = [ref for ref in claim.numbers if matches(written, ref)]
        if any(not grounding[ref] for ref in cited) or any(_quotes(written, p) for p in passages):
            continue
        if cited:
            reasons.append(f"figure: {written.token} cites {grounding[cited[0]]}")
        else:
            reasons.append(
                f"figure: {written.token} matches none of the claim's numbers{_closest(written, claim.numbers)}"
            )
    return reasons


def _source_reasons(claim: Claim, retrieved: Mapping[str, Hit], principal: Principal) -> list[str]:
    reasons = []
    for chunk_id in claim.citations:
        hit = retrieved.get(chunk_id)
        if hit is None:
            reasons.append(f"source: {chunk_id} wasn't retrieved for this question")
        elif hit.region is not None and hit.region not in principal.regions:
            # Retrieval runs as the asker, so this fires only when evidence was gathered as someone else.
            reasons.append(f"source: {chunk_id} is in the {hit.region}, outside what {principal.user_id} can see")
    return reasons


WORD = re.compile(r"[A-Za-z]+")


def _strings(value: Any, skip: frozenset[str], key: str = "") -> Iterator[str]:
    if key in skip:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for name, inner in value.items():
            yield from _strings(inner, skip, str(name))
    elif isinstance(value, list | tuple):
        for inner in value:
            yield from _strings(inner, skip, key)


def names_in(evidence: Evidence) -> frozenset[str]:
    """The capitalized words the evidence's own text holds, such as the surname Rose or the first name Max. Group
    values such as a row's region are left out: those are labels to check, not names."""
    grouping = frozenset({*default_layer().dimensions, "grp", "group", "period"})
    texts = _strings([evidence.rows, evidence.sandbox, evidence.scan_fields], grouping)
    return frozenset(word for text in texts for word in WORD.findall(text) if word[0].isupper())


def _compares(text: str, found: re.Match[str], names: frozenset[str]) -> bool:
    """Whether a listed word compares here, rather than naming something the evidence names or sitting in a phrase
    such as "made up" or "at least"."""
    word = found.group()
    if word[0].isupper() and word in names:
        return False
    before = WORD.findall(text[: found.start()])[-1:]
    return not before or (before[0].lower(), word.lower()) not in NOT_COMPARING


@dataclass(frozen=True)
class Change:
    """The two values a change ref compares, the later one first."""

    now: Decimal
    then: Decimal

    @property
    def way(self) -> str:
        return "rise" if self.now > self.then else "fall" if self.now < self.then else "no change"


def _parsed(derivation: str) -> ast.expr | None:
    try:
        return ast.parse(derivation, mode="eval").body
    except (SyntaxError, ValueError):
        return None


def _read_at_row(node: ast.expr) -> tuple[str, int] | None:
    """A quantity read at one row, such as value[0] or (num / den)[3], as (the quantity, the row)."""
    match node:
        case ast.Subscript(value=inner, slice=ast.Constant(value=int() as row)) if not isinstance(row, bool):
            return ast.dump(inner), row
    return None


def _change_sides(node: ast.expr) -> tuple[ast.expr, ast.expr] | None:
    """The later and earlier reads of a change: the same quantity at two rows, subtracted, as in value[0] - value[1],
    or that difference over the earlier read, the percent change."""
    match node:
        case ast.BinOp(left=ast.BinOp(op=ast.Sub()) as change, op=ast.Div(), right=base):
            sides = _change_sides(change)
            return sides if sides is not None and ast.dump(base) == ast.dump(sides[1]) else None
        case ast.BinOp(left=now, op=ast.Sub(), right=then):
            a, b = _read_at_row(now), _read_at_row(then)
            return (now, then) if a is not None and b is not None and a[0] == b[0] and a[1] != b[1] else None
    return None


class Reader:
    """Reads a claim's refs against the result rows, each once: the change a ref records, and the row value it reads.
    Only a derivation that recomputes to the ref's own value counts, and a sign comes from the rows, never the ref."""

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.rows = rows
        self._changes: dict[NumberRef, Change | None] = {}
        self._levels: dict[NumberRef, Decimal | None] = {}
        self._traced: dict[NumberRef, tuple[int, ...]] = {}

    def traced_rows(self, ref: NumberRef) -> tuple[int, ...]:
        """The rows a ref's value is read from, when its derivation recomputes to it: the rows it subscripts, or its
        row_index when it names columns alone. Empty for a value from sandbox outputs, scan fields or nowhere."""
        if ref not in self._traced:
            self._traced[ref] = self._traced_rows(ref)
        return self._traced[ref]

    def _traced_rows(self, ref: NumberRef) -> tuple[int, ...]:
        node = _parsed(ref.derivation)
        if node is None or _unanchored(node):
            return ()
        read = {at[1] for n in ast.walk(node) if isinstance(n, ast.expr) and (at := _read_at_row(n)) is not None}
        rows = read or ({ref.row_index} if ref.row_index is not None else set())
        try:
            computed = _evaluate(node, self.rows, ref.row_index)
        except (_Unresolved, ArithmeticError):
            return ()
        in_range = all(0 <= row < len(self.rows) for row in rows)
        return tuple(sorted(rows)) if rows and in_range and _same(computed, ref.value) else ()

    def change(self, ref: NumberRef) -> Change | None:
        if ref not in self._changes:
            self._changes[ref] = self._change(ref)
        return self._changes[ref]

    def level(self, ref: NumberRef) -> Decimal | None:
        """The ref's value, when it reads one row by column names alone, as value or num / den at row_index does."""
        if ref not in self._levels:
            self._levels[ref] = self._level(ref)
        return self._levels[ref]

    def _change(self, ref: NumberRef) -> Change | None:
        node = _parsed(ref.derivation)
        sides = _change_sides(node) if node is not None else None
        if node is None or sides is None or _unanchored(node):
            return None
        try:
            now, then = _evaluate(sides[0], self.rows, None), _evaluate(sides[1], self.rows, None)
            computed = _evaluate(node, self.rows, ref.row_index)
        except (_Unresolved, ArithmeticError):
            return None
        return Change(now, then) if _same(computed, ref.value) else None

    def _level(self, ref: NumberRef) -> Decimal | None:
        node = _parsed(ref.derivation)
        if node is None or ref.row_index is None or any(isinstance(n, ast.Subscript) for n in ast.walk(node)):
            return None
        if _unanchored(node):
            return None
        try:
            computed = _evaluate(node, self.rows, ref.row_index)
        except (_Unresolved, ArithmeticError):
            return None
        return computed if _same(computed, ref.value) else None

    def extreme(self, ref: NumberRef, *, most: bool) -> bool:
        """Whether a level ref is the largest, or the smallest, of its quantity over the rows like its own: the same
        columns, in the same period when the rows carry one. A tie counts."""
        value, node = self.level(ref), _parsed(ref.derivation)
        if value is None or node is None or ref.row_index is None:
            return False
        own = self.rows[ref.row_index]
        for index, row in enumerate(self.rows):
            if row.keys() != own.keys() or row.get("period") != own.get("period"):
                continue
            try:
                other = _evaluate(node, self.rows, index)
            except (_Unresolved, ArithmeticError):
                continue
            beyond = other - value if most else value - other
            if beyond > EXACT * max(Decimal(1), abs(value), abs(other)):
                return False
        return True


def _bears_out(word: str, change: Change) -> bool:
    if word in RISING:
        return change.now > change.then
    if word in FALLING:
        return change.now < change.then
    if change.then <= 0:
        return False
    # The word is read as the multiple it states, so "doubled" is a written 2 held to the multiple row's tolerance.
    stated = Written(word, MULTIPLES[word], "decimal", 0)
    return abs(change.now / change.then - stated.value) <= NORMALIZATION["multiple"].tolerance(stated)


def _says(cited: str, word: str) -> bool:
    return re.search(rf"\b{word}\b", cited, re.IGNORECASE) is not None


@dataclass(frozen=True)
class _Placed:
    start: int
    end: int
    written: Written
    refs: tuple[NumberRef, ...]  # the claim's refs the figure matches


def _placed(claim: Claim) -> list[_Placed]:
    """Each figure in the claim, where it sits in the text and the refs it matches."""
    blanked = NOT_QUANTITIES.sub(lambda m: " " * len(m.group()), claim.text)
    found = iter(figures(claim.text))
    placed, written = [], next(found, None)
    # figures() reads these same matches and skips years and identifiers, so each figure is the next match it quotes.
    for match in FIGURE.finditer(blanked):
        if written is not None and claim.text[match.start() : match.end()].strip() == written.token:
            refs = tuple(ref for ref in claim.numbers if matches(written, ref))
            placed.append(_Placed(match.start(), match.end(), written, refs))
            written = next(found, None)
    return placed


def _gap(figure: _Placed, word: re.Match[str]) -> int:
    return max(word.start() - figure.end, figure.start - word.end(), 0)


def _moving_reasons(
    moves: list[re.Match[str]], placed: list[_Placed], reader: Reader, numbers: tuple[NumberRef, ...], cited: str
) -> list[str]:
    """A direction or multiple word goes with the change figures written nearest it, and each must match a change that
    bears it out. With none near it, the changes the claim traces but doesn't write must all bear it out, or failing
    those every change it traces. Only a claim that traces no change may lean on a cited passage using the word."""
    # A figure that matches a value read at one row is a level, even if some change happens to be the same size.
    changed = [
        figure
        for figure in placed
        if any(reader.change(r) for r in figure.refs) and all(reader.level(r) is None for r in figure.refs)
    ]
    near: dict[int, list[_Placed]] = {}
    for figure in changed:
        near.setdefault(min(range(len(moves)), key=lambda i: (_gap(figure, moves[i]), i)), []).append(figure)
    written = {ref for figure in placed for ref in figure.refs}
    traced = [ref for ref in numbers if reader.change(ref) is not None]
    reasons = []
    for index, found in enumerate(moves):
        word, kind = found.group().lower(), "multiple" if found.group().lower() in MULTIPLES else "direction"
        if index in near:
            for figure in near[index]:
                changes = [c for r in figure.refs if (c := reader.change(r)) is not None]
                if not any(_bears_out(word, c) for c in changes):
                    reasons.append(_moving_reason(kind, word, figure.written.token, changes[0]))
            continue
        rest = [ref for ref in traced if ref not in written] or traced
        wrong = [c for r in rest if (c := reader.change(r)) is not None and not _bears_out(word, c)]
        if wrong:
            reasons.append(_moving_reason(kind, word, "the change the claim traces", wrong[0]))
        elif not rest and not _says(cited, word):
            reasons.append(f'{kind}: "{word}" isn\'t traced to a change in the data')
    return reasons


def _moving_reason(kind: str, word: str, what: str, change: Change) -> str:
    if kind == "direction":
        return f'direction: "{word}" goes with {what}, which is a {change.way} in the data'
    if change.then <= 0:
        return f'multiple: "{word}" goes with {what}, which starts from {change.then:,}, so no multiple applies'
    return f'multiple: "{word}" goes with {what}, which comes to {change.now / change.then:.2f} times the earlier value'


def _ranking_reasons(
    words: list[re.Match[str]], placed: list[_Placed], reader: Reader, numbers: tuple[NumberRef, ...], cited: str
) -> list[str]:
    """A superlative goes with the first row value written after it, or failing that the last before it, without
    crossing another comparative word, and that value must be the largest, or smallest, of its kind. With none beside
    it, the row values the claim traces but doesn't write must all be, or failing those every one it traces. Only a
    claim that traces no row value may lean on a cited passage using the word."""
    levels = [f for f in placed if any(reader.level(r) is not None for r in f.refs)]
    written = {ref for figure in placed for ref in figure.refs}
    traced = [ref for ref in numbers if reader.level(ref) is not None]
    reasons = []
    for index, found in enumerate(words):
        word = found.group().lower()
        if word not in HIGHEST and word not in LOWEST:
            continue
        most, largest = word in HIGHEST, "largest" if word in HIGHEST else "smallest"
        stop = words[index + 1].start() if index + 1 < len(words) else len(found.string)
        start = words[index - 1].end() if index else 0
        after = [f for f in levels if found.end() <= f.start and f.end <= stop]
        before = [f for f in levels if start <= f.start and f.end <= found.start()]
        figure = after[0] if after else before[-1] if before else None
        if figure is not None:
            if not any(reader.extreme(r, most=most) for r in figure.refs if reader.level(r) is not None):
                reasons.append(
                    f'superlative: "{word}" goes with {figure.written.token}, which isn\'t the {largest} '
                    "value of its kind in the evidence"
                )
            continue
        rest = [ref for ref in traced if ref not in written] or traced
        if rest and not all(reader.extreme(r, most=most) for r in rest):
            reasons.append(
                f'superlative: "{word}" rests on a value that isn\'t the {largest} of its kind in the evidence'
            )
        elif not rest and not _says(cited, word):
            reasons.append(f'superlative: "{word}" isn\'t traced to a figure in the data')
    return reasons


def _comparison_reasons(
    claim: Claim, placed: list[_Placed], reader: Reader, names: frozenset[str], cited: str
) -> list[str]:
    """Comparative words must be borne out by the numbers the claim traces."""
    words = [found for found in COMPARATIVE.finditer(claim.text) if _compares(claim.text, found, names)]
    if not words:
        return []
    moves = [found for found in words if found.group().lower() not in (*HIGHEST, *LOWEST)]
    return [
        *(_moving_reasons(moves, placed, reader, claim.numbers, cited) if moves else []),
        *_ranking_reasons(words, placed, reader, claim.numbers, cited),
    ]


GRAINS = ("month", "quarter", "year")
# Where a figure's clause ends: sentence and list punctuation, the words the writers join clauses with, and the "to"
# of a range, as in "from 9 in Jul 2025 to 314 in Jan 2025".
CLAUSE_BREAK = re.compile(r"[.;!?](?=\s|$)|,\s|\n|\s(?:and|while|but|whereas|to)\s", re.IGNORECASE)
MONTH_NUMBERS = {
    **{name.lower(): number for number, name in enumerate(MONTHS, 1)},
    **{name[:3].lower(): number for number, name in enumerate(MONTHS, 1)},
    "sept": 9,
}
PERIOD = re.compile(
    rf"\bQ([1-4])\s+(\d{{4}})\b|\b({'|'.join(sorted(MONTH_NUMBERS, key=len, reverse=True))})\.?\s+(\d{{4}})\b"
    r"|(?<![\d/-])((?:19|20)\d{2})(?![\d/-])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Label:
    """A group or period an answer names: a region, state code, peril, channel or status, or a month, quarter or year
    with its first and last day."""

    text: str
    start: int
    end: int
    dim: str
    value: str
    days: tuple[date, date] | None = None


@cache
def _vocabulary() -> tuple[re.Pattern[str], re.Pattern[str], dict[str, tuple[str, str]]]:
    """Every way the semantic layer names a group: its values and synonyms, and each state's name and code."""
    words: dict[str, tuple[str, str]] = {}
    codes: dict[str, tuple[str, str]] = {}
    for dim in default_layer().dimensions.values():
        for value in dim.values:
            if len(value) <= 3 and value.isupper():
                codes[value] = (dim.name, value)
            else:
                words[value.lower()] = (dim.name, value)
        words.update({text.lower(): (dim.name, value) for text, value in dim.synonyms.items()})
    words.update({name.lower(): ("state", code) for code, name in state_names().items()})
    phrases = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    return re.compile(rf"\b(?:{phrases})\b", re.IGNORECASE), re.compile(rf"\b(?:{'|'.join(codes)})\b"), words | codes


def _labels(text: str, names: frozenset[str]) -> list[Label]:
    """The groups and periods a text names, leaving out names the evidence holds, such as the surname West."""
    found = []
    for match in PERIOD.finditer(text):
        if match[1]:
            year, first, dim = int(match[2]), 3 * int(match[1]) - 2, "quarter"
            days = (date(year, first, 1), month_end(year, first + 2))
        elif match[3]:
            year, month, dim = int(match[4]), MONTH_NUMBERS[match[3].lower()], "month"
            days = (date(year, month, 1), month_end(year, month))
        else:
            year, dim = int(match[5]), "year"
            days = (date(year, 1, 1), date(year, 12, 31))
        found.append(Label(match.group(), match.start(), match.end(), dim, str(days[0]), days))
    words, codes, meaning = _vocabulary()
    for match in (*words.finditer(text), *codes.finditer(text)):
        taken = any(match.start() < label.end and label.start < match.end() for label in found)
        if taken or match.group() in names:
            continue
        dim, value = meaning.get(match.group().lower()) or meaning[match.group()]
        found.append(Label(match.group(), match.start(), match.end(), dim, value))
    return found


def _clause(text: str, at: int) -> tuple[int, int]:
    start, end = 0, len(text)
    for found in CLAUSE_BREAK.finditer(text):
        if found.group().strip().lower() == "to" and " from " not in text[start : found.start()].lower():
            continue  # "came to 79" is one clause
        if found.end() <= at:
            start = found.end()
        elif found.start() >= at:
            end = found.start()
            break
    return start, end


def _row_value(row: Mapping[str, Any], dim: str) -> Any:
    """A row's value for a dimension, as a column of its own or inside the aggregate function's grp."""
    if dim in row:
        return row[dim]
    group = row.get("grp")
    return group.get(dim) if isinstance(group, Mapping) else None


def _grain_days(grain: str, value: Any) -> tuple[date, date] | None:
    """The first and last day of a row's month, quarter or year, from a date or from the aggregate's 2025-07,
    2025-Q2 or 2025."""
    if isinstance(value, date):
        year, month = value.year, value.month
    elif isinstance(value, str) and (parts := re.fullmatch(r"(\d{4})(?:-(\d{2})|-Q([1-4]))?", value)):
        year = int(parts[1])
        month = int(parts[2]) if parts[2] else 3 * int(parts[3]) - 2 if parts[3] else 1
    else:
        return None
    if grain == "month":
        return date(year, month, 1), month_end(year, month)
    if grain == "quarter":
        first = month - (month - 1) % 3
        return date(year, first, 1), month_end(year, first + 2)
    return date(year, 1, 1), date(year, 12, 31)


def _fits(label: Label, rows: list[Mapping[str, Any]]) -> tuple[bool | None, str]:
    """Whether rows that carry the label's dimension all hold its value, and what a row that doesn't holds instead.
    None when no row carries the dimension. A period fits a row's month, quarter or year when the two overlap."""
    seen: list[tuple[bool, str]] = []
    for row in rows:
        if label.days is not None:
            for grain in GRAINS:
                if (days := _grain_days(grain, value := _row_value(row, grain))) is not None:
                    fits = label.days[0] <= days[1] and days[0] <= label.days[1]
                    seen.append((fits, f"{grain} {group_label(grain, value)}"))
                    break
        elif (value := _row_value(row, label.dim)) is not None:
            seen.append((str(value).lower() == label.value.lower(), f"{label.dim} {group_label(label.dim, value)}"))
    if not seen:
        return None, ""
    wrong = next((shown for fits, shown in seen if not fits), None)
    return wrong is None, wrong or ""


def _label_reasons(
    claim: Claim, placed: list[_Placed], reader: Reader, names: frozenset[str], passages: tuple[Written, ...]
) -> list[str]:
    """A figure traced to rows goes with the group or period of each dimension named nearest it in its own clause,
    and the rows that carry that dimension must hold that value. A row that doesn't carry it, because the question
    filtered on it rather than grouping by it, isn't checked: the evidence doesn't record the filters. A figure the
    cited passage quotes is the passage's, not a row's."""
    traced = []
    for figure in placed:
        rows = [found for ref in figure.refs if (found := reader.traced_rows(ref))]
        if rows and not any(_quotes(figure.written, passage) for passage in passages):
            traced.append((figure, rows))
    labels = _labels(claim.text, names) if traced else []
    reasons = []
    for figure, rows in traced:
        start, end = _clause(claim.text, figure.start)
        near = [label for label in labels if start <= label.start and label.end <= end]
        # The nearest label of each dimension, one before the figure winning a tie.
        attached: dict[str, Label] = {}
        for label in sorted(near, key=lambda lb: (_span_gap(lb, figure), lb.start > figure.start)):
            attached.setdefault(label.dim, label)
        for label in attached.values():
            verdicts = [_fits(label, [reader.rows[i] for i in read]) for read in rows]
            # A figure matching several refs holds when any of them fits the label, or can't be checked against it.
            if all(fits is False for fits, _ in verdicts):
                reasons.append(
                    f'label: "{label.text}" does not match the row {figure.written.token} traces to, which has '
                    f"{verdicts[0][1]}"
                )
    return reasons


def _span_gap(label: Label, figure: _Placed) -> int:
    return max(label.start - figure.end, figure.start - label.end, 0)


SecondCheck = Callable[[Claim, str], bool]
Composer = Callable[[Evidence, tuple[str, ...]], Draft | Awaitable[Draft]]


@dataclass(frozen=True)
class ClaimCheck:
    claim: Claim
    supported: bool
    reasons: tuple[str, ...]
    sources: tuple[Hit, ...] = ()  # the retrieved hits the claim cites, which become the answer's citations


@dataclass(frozen=True)
class Verification:
    checks: tuple[ClaimCheck, ...]
    passed: bool


def verify(
    draft: Draft, evidence: Evidence, principal: Principal, *, second_check: SecondCheck | None = None
) -> Verification:
    """second_check is the live reading of a claim against the text it cites; it runs only on cited claims that
    already passed the figure and source checks."""
    pool = evidence_values(evidence)
    reader, names = Reader(evidence.rows), names_in(evidence)
    retrieved = {hit.chunk_id: hit for hit in evidence.hits}
    checks = []
    for claim in draft.claims:
        sources = tuple(retrieved[c] for c in dict.fromkeys(claim.citations) if c in retrieved)
        cited = "\n\n".join("\n".join(filter(None, (hit.header, hit.body))) for hit in sources)
        passages = figures(cited) if cited else ()
        placed = _placed(claim)
        reasons = [
            *_figure_reasons(claim, evidence.rows, pool, passages),
            *_source_reasons(claim, retrieved, principal),
            *_comparison_reasons(claim, placed, reader, names, cited),
            *_label_reasons(claim, placed, reader, names, passages),
        ]
        if not reasons and sources and second_check is not None and not second_check(claim, cited):
            reasons.append("reading: the cited text doesn't bear the claim out")
        checks.append(ClaimCheck(claim, not reasons, tuple(reasons), sources))
    return Verification(tuple(checks), bool(checks) and all(check.supported for check in checks))


# What the asker reads for each cut claim. The claim's own words stay out: its figure is wrong, or its source
# isn't theirs to read.
COULD_NOT_CONFIRM = {
    "figure": "One statement had a figure I couldn't match to the data, so I left it out.",
    "source": "One statement cited a source that wasn't among the documents found for you, so I left it out.",
    "reading": "One statement wasn't borne out by the document it cited, so I left it out.",
    "direction": "One statement said a figure rose or fell in a way the data doesn't show, so I left it out.",
    "superlative": "One statement ranked a figure in a way the data doesn't show, so I left it out.",
    "label": "One statement put a figure under a group or period the data doesn't give it, so I left it out.",
    "multiple": (
        "One statement said a figure doubled, tripled or halved, which the data doesn't show, so I left it out."
    ),
}
UNCONFIRMED = "One statement couldn't be confirmed, so I left it out."
NO_ANSWER = "I couldn't confirm an answer from the data and documents available."


def finalize(draft: Draft, verification: Verification) -> Answer:
    if tuple(check.claim for check in verification.checks) != draft.claims:
        raise ValueError("the verification is for a different draft")
    kept = [check for check in verification.checks if check.supported]
    cut = [check for check in verification.checks if not check.supported]
    lines = tuple(
        COULD_NOT_CONFIRM.get(check.reasons[0].partition(":")[0], UNCONFIRMED) if check.reasons else UNCONFIRMED
        for check in cut
    )
    if kept:
        parts = [check.claim.text for check in kept] + list(draft.caveats)
        text = ("\n" if any(part.startswith("- ") or "\n" in part for part in parts) else " ").join(parts)
    else:
        text = NO_ANSWER
    citations = tuple(dict.fromkeys(hit for check in kept for hit in check.sources))
    return Answer(text, tuple(c.claim for c in kept), tuple(c.claim for c in cut), lines, citations)


async def _drafted(made: Draft | Awaitable[Draft]) -> Draft:
    return made if isinstance(made, Draft) else await made


async def verify_and_retry(
    compose_fn: Composer, evidence: Evidence, principal: Principal, *, second_check: SecondCheck | None = None
) -> Answer:
    """Composes, and when a claim fails, composes once more with every failing reason before keeping what holds.
    A deterministic composer ignores the feedback and returns the same draft, so its retry changes nothing."""
    draft = await _drafted(compose_fn(evidence, ()))
    verification = verify(draft, evidence, principal, second_check=second_check)
    if not verification.passed:
        feedback = tuple(f'"{check.claim.text}": {reason}' for check in verification.checks for reason in check.reasons)
        draft = await _drafted(compose_fn(evidence, feedback))
        verification = verify(draft, evidence, principal, second_check=second_check)
    return finalize(draft, verification)
