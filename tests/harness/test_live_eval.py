import copy
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app import events as ev
from app.llm.client import LLM, Response, Unavailable, Usage, ask
from app.llm.fake import reply
from app.llm.request import Message, Request
from app.memory import Memory
from app.replay import Logged
from evals import live, report, run
from evals.live import Metered
from evals.metrics import wilson
from evals.outcome import Outcome


def rate(hits: int, n: int) -> dict[str, float | int]:
    return wilson(hits, n).as_dict()


class Client:
    """A live client whose every reply reports the same tokens and, call by call, the costs the test gives it."""

    def __init__(
        self,
        *costs: Decimal | None,
        fast: str = "fast-1",
        main: str = "main-1",
        provider: str = "fake",
        checker: LLM | None = None,
        error: Exception | None = None,
    ) -> None:
        self._costs = list(costs)
        self._fast, self._main, self._provider, self._checker = fast, main, provider, checker
        self._error = error
        self.requests: list[Request] = []

    @property
    def fast_model(self) -> str:
        return self._fast

    @property
    def main_model(self) -> str:
        return self._main

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def checker(self) -> LLM:
        return self if self._checker is None else self._checker

    async def complete(self, request: Request) -> Response:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        cost = self._costs.pop(0) if self._costs else Decimal("0.001")
        usage = Usage(input_tokens=100, output_tokens=20, cache_read=10, cache_write=5, cost_usd=cost)
        return replace(reply("ok"), model=request.model, usage=usage)


def request(model: str) -> Request:
    return Request("test.v1", model, ("Reply with ok.",), (Message("user", ("ping",)),), 16)


def used(calls: int, failed: int = 0, cost: str | None = "0") -> dict[str, Any]:
    tokens = calls - failed
    return {
        "calls": calls,
        "failed": failed,
        "input_tokens": 100 * tokens,
        "output_tokens": 20 * tokens,
        "cache_read_tokens": 10 * tokens,
        "cache_write_tokens": 5 * tokens,
        "cost_usd": None if cost is None else Decimal(cost),
    }


async def test_the_meter_counts_each_models_calls_tokens_and_cost() -> None:
    metered = Metered(Client(Decimal("0.0012"), Decimal("0.0012"), Decimal("0.03")))
    await ask(metered, request(metered.fast_model))
    await ask(metered, request(metered.fast_model))
    await ask(metered, request(metered.main_model))
    assert metered.spent() == {"fast-1": used(2, cost="0.0024"), "main-1": used(1, cost="0.03")}
    assert metered.cost_usd == Decimal("0.0324")


async def test_a_call_with_no_known_cost_leaves_its_models_cost_and_the_total_unknown_never_zero() -> None:
    metered = Metered(Client(None, Decimal("0.5"), Decimal("0.25")))
    await ask(metered, request("main-1"))
    await ask(metered, request("main-1"))
    await ask(metered, request("fast-1"))
    spent = metered.spent()
    assert spent["main-1"]["cost_usd"] is None and spent["main-1"]["calls"] == 2
    assert spent["fast-1"]["cost_usd"] == Decimal("0.25")
    assert metered.cost_usd is None


async def test_a_call_that_raises_is_counted_as_failed_and_adds_no_usage() -> None:
    metered = Metered(Client(error=Unavailable("test.v1: APIConnectionError")))
    with pytest.raises(Unavailable):
        await ask(metered, request("fast-1"))
    assert metered.spent() == {"fast-1": used(1, failed=1)}
    assert metered.cost_usd == Decimal(0)


async def test_the_meter_hands_out_everything_else_from_the_client_it_wraps() -> None:
    inner = Client(fast="haiku", main="sonnet", provider="gcp.vertex_ai")
    metered = Metered(inner)
    assert (metered.fast_model, metered.main_model, metered.provider) == ("haiku", "sonnet", "gcp.vertex_ai")
    await ask(metered, request("haiku"))
    assert metered.requests is inner.requests and len(inner.requests) == 1
    assert metered.checker is metered


async def test_a_checker_on_another_client_is_metered_into_the_same_counts() -> None:
    reader = Client(Decimal("0.002"), fast="gemini-flash", main="gemini-flash", provider="gcp.vertex_ai")
    metered = Metered(Client(Decimal("0.01"), checker=reader))
    checker = metered.checker
    assert checker is not metered and checker is metered.checker
    assert (checker.provider, checker.fast_model) == ("gcp.vertex_ai", "gemini-flash")
    await ask(checker, request(checker.fast_model))
    await ask(metered, request(metered.fast_model))
    assert metered.spent() == {"fast-1": used(1, cost="0.01"), "gemini-flash": used(1, cost="0.002")}
    assert len(reader.requests) == 1
    assert metered.cost_usd == Decimal("0.012")


def test_summarize_keeps_every_runs_value_in_order_and_the_lowest_and_highest() -> None:
    runs: list[dict[str, Any]] = [
        {"dev": {"routing": {"accuracy": rate(33, 34), "macro_f1": 0.97}, "permissions": {"leaks": 0}}},
        {"dev": {"routing": {"accuracy": rate(31, 34), "macro_f1": 0.94}, "permissions": {"leaks": 0}}},
        {"dev": {"routing": {"accuracy": rate(34, 34), "macro_f1": 1.0}, "permissions": {"leaks": 0}}},
    ]
    summary = live.summarize(runs)
    assert summary["dev"]["routing"]["accuracy"] == {
        "values": [0.9706, 0.9118, 1.0],
        "min": 0.9118,
        "max": 1.0,
        "hits": [33, 31, 34],
        "n": [34, 34, 34],
    }
    assert summary["dev"]["routing"]["macro_f1"] == {"values": [0.97, 0.94, 1.0], "min": 0.94, "max": 1.0}
    assert summary["dev"]["permissions"]["leaks"] == {"values": [0, 0, 0], "min": 0, "max": 0}


def test_summarize_keeps_splits_apart_and_marks_a_run_that_lacked_a_number() -> None:
    runs = [
        {"dev": {"sql": {"execution_accuracy": rate(20, 24)}}, "heldout": {"sql": {"execution_accuracy": rate(9, 12)}}},
        {"dev": {}, "heldout": {"sql": {"execution_accuracy": rate(10, 12)}}},
    ]
    summary = live.summarize(runs)
    dev, heldout = summary["dev"]["sql"]["execution_accuracy"], summary["heldout"]["sql"]["execution_accuracy"]
    assert dev == {"values": [0.8333, None], "min": 0.8333, "max": 0.8333, "hits": [20, None], "n": [24, None]}
    assert heldout["values"] == [0.75, 0.8333] and heldout["hits"] == [9, 10]


def outcome(case_id: str, *events: ev.Event) -> Outcome:
    logged = Logged(list(events), [], None, Memory(), "session")
    return Outcome({"id": case_id, "q": "question", "user": "dana"}, "dana", logged)


def test_fallbacks_are_read_from_the_live_event_each_request_logs() -> None:
    done = ev.Done("r", 1.0, "why", "answer")
    outcomes = [
        outcome("a", ev.Live("time"), done),
        outcome("b", ev.Live(None, retried=True), done),
        outcome("c", done),
        outcome("d", ev.Live("model"), done),
        outcome("e", ev.Live("time"), done),
    ]
    assert live.score_fallbacks(outcomes) == {"requests": rate(3, 5), "reasons": {"model": 1, "time": 2}}


def test_a_live_runs_numbers_are_the_headline_keys_with_rates_whole_and_the_fallback_rate() -> None:
    section = {
        "routing": {"accuracy": rate(3, 4), "macro_f1": 0.8, "misrouted": ["r-1"]},
        "sql": {"execution_accuracy": rate(2, 2)},
        "latency": {"why": {"n": 1, "p50_ms": 900}},
        "fallbacks": {"requests": rate(1, 6), "reasons": {"model": 1}},
    }
    assert live.numbers(section) == {
        "routing": {"accuracy": rate(3, 4), "macro_f1": 0.8},
        "sql": {"execution_accuracy": rate(2, 2)},
        "fallbacks": {"requests": rate(1, 6)},
    }
    assert report.headline(section, {}) == {
        "routing": {"accuracy": 0.75, "macro_f1": 0.8},
        "sql": {"execution_accuracy": 1.0},
    }


def scored(*hits: int, reasons: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """Held-out sections for a run each, whose figure answers got these hits of 12."""
    fell = reasons or {}
    return [
        {
            "heldout": {
                "sql": {"execution_accuracy": rate(h, 12)},
                "fallbacks": {"requests": rate(sum(fell.values()), 12), "reasons": fell},
            }
        }
        for h in hits
    ]


async def test_the_live_section_names_the_models_the_runs_and_what_they_cost() -> None:
    reader = Client(fast="gemini-flash", main="gemini-flash")
    metered = Metered(Client(Decimal("0.0100006"), checker=reader))
    await ask(metered, request("main-1"))
    found = live.section(metered, scored(9, 11, reasons={"time": 1}), "abc1234")
    assert found["date"] == datetime.now(UTC).date().isoformat()
    assert {k: found[k] for k in ("commit", "backend", "models", "runs", "splits")} == {
        "commit": "abc1234",
        "backend": "fake",
        "models": {"fast": "fast-1", "main": "main-1", "check": "gemini-flash"},
        "runs": 2,
        "splits": ["heldout"],
    }
    assert found["metrics"]["heldout"]["sql"]["execution_accuracy"]["hits"] == [9, 11]
    assert found["metrics"]["heldout"]["fallbacks"]["requests"]["values"] == [0.0833, 0.0833]
    assert found["fallback_reasons"] == {"heldout": {"time": 2}}
    assert found["usage"] == {"main-1": used(1, cost="0.010001")}
    assert found["cost_usd"] == Decimal("0.010001")
    # Decimals go into the report as plain JSON numbers.
    assert json.loads(report.dump({"live": found}))["live"]["cost_usd"] == 0.010001


def test_live_with_check_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        run.main(["--live", "--check"])
    assert exited.value.code == 2
    assert "--live can't be used with --check" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "said"),
    [
        (["--write", "--runs", "3"], "--runs counts live runs, so it needs --live"),
        (["--live", "--write", "--runs", "0"], "--runs must be from 1 to 5"),
        (["--live", "--write", "--runs", "6"], "--runs must be from 1 to 5"),
    ],
)
def test_runs_needs_live_and_one_to_five_runs(argv: list[str], said: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        run.main(argv)
    assert exited.value.code == 2
    assert said in capsys.readouterr().err


def test_live_with_live_mode_off_says_so_and_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    assert run.main(["--live", "--write"]) == 2
    assert "LLM_BACKEND is off" in capsys.readouterr().err


def test_live_with_settings_that_dont_hold_says_which_and_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LLM_BACKEND", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_CHECK_BACKEND", raising=False)
    assert run.main(["--live", "--write"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


COMMITTED: dict[str, Any] = {
    "heldout": {"sql": {"execution_accuracy": rate(9, 12)}, "latency": {"why": {"n": 3, "p50_ms": 900}}},
    "shared": {"hostile_sql": {"harmful": 0}},
    "live": {"runs": 3, "splits": ["heldout"], "cost_usd": 1.25, "metrics": {"heldout": {}}},
}


@pytest.fixture
def committed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A committed report in a scratch repo, with the lock and preflight checks passed."""
    path = tmp_path / "evals" / "report.json"
    path.parent.mkdir()
    path.write_text(report.dump(COMMITTED))
    monkeypatch.setattr(report, "REPORT", path)
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(run, "verify_lock", lambda: None)
    monkeypatch.setattr(run, "preflight", lambda: [])
    return path


def fresh_heldout(hits: int) -> dict[str, Any]:
    fresh = {key: copy.deepcopy(COMMITTED[key]) for key in ("heldout", "shared")}
    fresh["heldout"]["sql"]["execution_accuracy"] = rate(hits, 12)
    return fresh


def returning(fresh: dict[str, Any]) -> Any:
    async def collect(splits: Sequence[str]) -> dict[str, Any]:
        return fresh

    return collect


def test_a_no_key_write_keeps_the_committed_live_section(committed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run, "collect", returning(fresh_heldout(10)))
    assert run.main(["--write", "--split", "heldout"]) == 0
    written = json.loads(committed.read_text())
    assert written["heldout"]["sql"]["execution_accuracy"] == rate(10, 12)
    assert written["live"] == COMMITTED["live"]


def test_check_compares_the_no_key_sections_and_never_the_live_one(
    committed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run, "collect", returning(fresh_heldout(9)))
    assert run.main(["--check", "--split", "heldout"]) == 0
    monkeypatch.setattr(run, "collect", returning(fresh_heldout(8)))
    assert run.main(["--check", "--split", "heldout"]) == 1
    other_live = {**COMMITTED, "live": {"runs": 5, "cost_usd": None}}
    assert report.compare(COMMITTED, other_live, ["dev", "heldout", "shared"]) == []
    assert report.compare(COMMITTED, {k: v for k, v in COMMITTED.items() if k != "live"}, ["heldout", "shared"]) == []


def live_run(client: Client, *hits: int, dead: bool = False) -> Any:
    """Stands in for the live collection: every run calls the model once, as a case would, and scores the splits."""

    async def collect_live(splits: Sequence[str], llm: LLM, runs: int) -> list[dict[str, Any]]:
        assert isinstance(llm, Metered) and llm.inner is client and runs == len(hits)
        for _ in range(runs):
            await ask(llm, request(llm.main_model))
        sections = scored(*hits, reasons={"model": 1})
        if dead:
            for section in sections:
                section["dev"] = {"permissions": {"leaks": 0, "controls": {"own_notes_in_answers": 0}}}
        return sections

    return collect_live


def test_a_live_write_replaces_the_live_section_and_leaves_every_other_one_as_committed(
    committed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = Client(Decimal("0.02"), Decimal("0.03"))
    monkeypatch.setattr(run, "from_env", lambda: client)
    monkeypatch.setattr(run, "collect_live", live_run(client, 9, 11))
    before = json.loads(committed.read_text())
    assert run.main(["--live", "--write", "--runs", "2", "--split", "heldout"]) == 0
    after = json.loads(committed.read_text())
    assert {k: v for k, v in after.items() if k != "live"} == {k: v for k, v in before.items() if k != "live"}
    found = after["live"]
    assert (found["runs"], found["splits"], found["cost_usd"]) == (2, ["heldout"], 0.05)
    assert found["usage"]["main-1"]["calls"] == 2
    assert found["metrics"]["heldout"]["sql"]["execution_accuracy"]["hits"] == [9, 11]
    assert found["fallback_reasons"] == {"heldout": {"model": 2}}
    assert "2 live runs on fake came to $0.0500" in capsys.readouterr().out


def test_a_live_run_whose_leak_control_saw_nothing_is_not_written(
    committed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = Client()
    monkeypatch.setattr(run, "from_env", lambda: client)
    monkeypatch.setattr(run, "collect_live", live_run(client, 9, dead=True))
    before = committed.read_text()
    assert run.main(["--live", "--write", "--runs", "1"]) == 1
    assert committed.read_text() == before
    assert "refusing to report zero leaks that no control backs" in capsys.readouterr().err


def test_a_write_that_fails_halfway_leaves_the_committed_report_whole(
    committed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = committed.read_text()

    def broken(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.fsync", broken)
    with pytest.raises(OSError):
        report.write({"dev": {}})
    assert committed.read_text() == before
    assert [p.name for p in committed.parent.iterdir() if p.name.startswith(".report-")] == []
