"""Live mode scored against a real model.

make eval-live asks the eval cases with live mode on, several times over, since a model's answers vary from run to
run, and writes what it found into the live section of evals/report.json. Nothing else in the report changes. CI never
calls a model: make eval-check compares only the no-key sections, and tools/recount.py renders the live section from
whatever the committed report holds.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app import events as ev
from app.llm.client import LLM, Response
from app.llm.request import Request
from evals import report
from evals.outcome import Outcome, rate

RUNS = 3
# Every run pays for its model calls, so a typo like RUNS=100 is refused rather than billed.
MAX_RUNS = 5
# Costs are summed exactly and reported to a millionth of a dollar.
COST_PLACES = Decimal("0.000001")


@dataclass
class ModelUsage:
    """One model's calls and what their replies say they used. A call that raised has no usage to add."""

    calls: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # None from the first reply whose cost is unknown on, so a model with no list price never reads as free.
    cost_usd: Decimal | None = Decimal(0)

    def add(self, response: Response) -> None:
        usage = response.usage
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read
        self.cache_write_tokens += usage.cache_write
        if self.cost_usd is not None:
            self.cost_usd = None if usage.cost_usd is None else self.cost_usd + usage.cost_usd

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "failed": self.failed,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": None if self.cost_usd is None else self.cost_usd.quantize(COST_PLACES),
        }


class Metered:
    """A live client that counts, per model asked for, each call and the tokens and cost its reply reports. Everything
    else is the wrapped client's. The checker it hands out is metered into the same counts, so a support check on
    another model family is paid for in the same table."""

    def __init__(self, inner: LLM, usage: dict[str, ModelUsage] | None = None) -> None:
        self._inner = inner
        self.usage: dict[str, ModelUsage] = {} if usage is None else usage
        self._checker: Metered | None = None

    @property
    def inner(self) -> LLM:
        return self._inner

    @property
    def fast_model(self) -> str:
        return self._inner.fast_model

    @property
    def main_model(self) -> str:
        return self._inner.main_model

    @property
    def provider(self) -> str:
        return self._inner.provider

    @property
    def checker(self) -> LLM:
        reader = self._inner.checker
        if reader is self._inner:
            return self
        if self._checker is None or self._checker.inner is not reader:
            self._checker = Metered(reader, self.usage)
        return self._checker

    async def complete(self, request: Request) -> Response:
        counts = self.usage.setdefault(request.model, ModelUsage())
        counts.calls += 1
        try:
            response = await self._inner.complete(request)
        except BaseException:
            counts.failed += 1
            raise
        counts.add(response)
        return response

    def __getattr__(self, name: str) -> Any:
        # Anything else the wrapped client offers, such as a scripted model's record of its requests.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    @property
    def cost_usd(self) -> Decimal | None:
        """Every call's cost together, or None when any model's cost is unknown."""
        total = Decimal(0)
        for counts in self.usage.values():
            if counts.cost_usd is None:
                return None
            total += counts.cost_usd
        return total

    def spent(self) -> dict[str, dict[str, Any]]:
        return {model: counts.as_dict() for model, counts in sorted(self.usage.items())}


def score_fallbacks(outcomes: Sequence[Outcome]) -> dict[str, Any]:
    """Requests where a live step didn't hold and the no-key answer stood, read from the Live event the pipeline
    logs, and how often each reason came up."""
    reasons: Counter[str] = Counter()
    for o in outcomes:
        reason = next((e.fallback for e in o.logged.events if isinstance(e, ev.Live) and e.fallback), None)
        if reason is not None:
            reasons[reason] += 1
    return {"requests": rate(reasons.total(), len(outcomes)), "reasons": dict(sorted(reasons.items()))}


def numbers(section: Mapping[str, Any]) -> dict[str, Any]:
    """One split of one live run in the no-key headline's keys, each rate whole, and how often live mode fell back."""
    found = report.headline(dict(section), {}, whole=True)
    fallbacks = section.get("fallbacks")
    if isinstance(fallbacks, dict) and "requests" in fallbacks:
        found["fallbacks"] = {"requests": fallbacks["requests"]}
    return found


def _keys(maps: Sequence[Mapping[str, Any]]) -> list[str]:
    return list(dict.fromkeys(key for found in maps for key in found))


def _is_rate(found: Any) -> bool:
    return isinstance(found, dict) and {"hits", "n", "value"} <= found.keys()


def _number(found: Any) -> Any:
    """A rate's value, or the number itself. Anything else holds no number to compare."""
    if _is_rate(found):
        return found["value"]
    return None if isinstance(found, dict) else found


def _spread(found: Sequence[Any]) -> dict[str, Any] | None:
    values = [_number(x) for x in found]
    present = [v for v in values if v is not None]
    if not present:
        return None
    spread: dict[str, Any] = {"values": values, "min": min(present), "max": max(present)}
    if any(_is_rate(x) for x in found):
        spread["hits"] = [x["hits"] if _is_rate(x) else None for x in found]
        spread["n"] = [x["n"] if _is_rate(x) else None for x in found]
    return spread


def summarize(runs: Sequence[Mapping[str, Mapping[str, Mapping[str, Any]]]]) -> dict[str, Any]:
    """Each split's numbers over the runs, {split: {group: {key: spread}}} in the headline's groups and keys. A spread
    holds every run's value in run order, None where that run lacked it, and the lowest and highest. A rate is
    compared by its value and keeps each run's hits and n beside it, so it can be read as x of n."""
    summary: dict[str, Any] = {}
    for split in _keys(runs):
        per_run = [run.get(split, {}) for run in runs]
        for group in _keys(per_run):
            groups = [found.get(group, {}) for found in per_run]
            for key in _keys(groups):
                spread = _spread([found.get(key) for found in groups])
                if spread is not None:
                    summary.setdefault(split, {}).setdefault(group, {})[key] = spread
    return summary


def section(llm: Metered, runs: Sequence[Mapping[str, Mapping[str, Any]]], commit: str | None) -> dict[str, Any]:
    """The report's live section, from each run's scored splits and what the metered client counted over them all."""
    splits = _keys(runs)
    reasons: dict[str, Counter[str]] = {split: Counter() for split in splits}
    for run in runs:
        for split, scored in run.items():
            reasons[split].update(scored.get("fallbacks", {}).get("reasons", {}))
    cost = llm.cost_usd
    return {
        "date": datetime.now(UTC).date().isoformat(),
        "commit": commit,
        "backend": llm.provider,
        "models": {"fast": llm.fast_model, "main": llm.main_model, "check": llm.checker.fast_model},
        "runs": len(runs),
        "splits": splits,
        "metrics": summarize([{split: numbers(scored) for split, scored in run.items()} for run in runs]),
        "fallback_reasons": {split: dict(sorted(counts.items())) for split, counts in reasons.items()},
        "usage": llm.spent(),
        "cost_usd": None if cost is None else cost.quantize(COST_PLACES),
    }


def describe(live: Mapping[str, Any]) -> str:
    """What the live runs called and spent, a line per model and one for the total."""
    lines = []
    for model, used in live["usage"].items():
        cost = "an unknown cost" if used["cost_usd"] is None else f"${used['cost_usd']:,.4f}"
        failed = f" ({used['failed']} failed)" if used["failed"] else ""
        tokens = f"{used['input_tokens']:,} tokens in, {used['output_tokens']:,} out"
        lines.append(f"{model}: {used['calls']:,} calls{failed}, {tokens}, {cost}")
    total = "an unknown cost" if live["cost_usd"] is None else f"${live['cost_usd']:,.4f}"
    lines.append(f"{live['runs']} live runs on {live['backend']} came to {total}")
    return "\n".join(lines)
