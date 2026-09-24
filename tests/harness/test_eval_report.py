import copy
import json
from typing import Any

from evals import report
from evals.metrics import wilson


def rate(hits: int, n: int) -> dict[str, float | int]:
    return wilson(hits, n).as_dict()


BASE: dict[str, Any] = {
    "dev": {
        "sql": {"execution_accuracy": rate(20, 24)},
        "retrieval": {"hybrid": {"recall_at_5": rate(16, 17), "mrr_at_10": 0.9055}},
        "latency": {"why": {"n": 4, "p50_ms": 900}},
    },
    "heldout": {"sql": {"execution_accuracy": rate(9, 12)}},
    "shared": {"hostile_sql": {"harmful": 0}},
}


def changed(path: list[str], value: Any) -> dict[str, Any]:
    fresh = copy.deepcopy(BASE)
    node = fresh
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return fresh


def keys(fresh: dict[str, Any], sections: tuple[str, ...] = ("dev", "heldout", "shared")) -> list[str]:
    return [d.key for d in report.compare(BASE, fresh, list(sections))]


def test_a_deterministic_key_must_match_exactly() -> None:
    assert "dev.sql.execution_accuracy.value" in keys(changed(["dev", "sql", "execution_accuracy"], rate(19, 24)))
    assert keys(changed(["shared", "hostile_sql", "harmful"], 1)) == ["shared.hostile_sql.harmful"]


def test_an_embedding_key_may_move_by_up_to_two_hundredths() -> None:
    assert keys(changed(["dev", "retrieval", "hybrid", "mrr_at_10"], 0.92)) == []
    assert keys(changed(["dev", "retrieval", "hybrid", "mrr_at_10"], 0.93)) == ["dev.retrieval.hybrid.mrr_at_10"]


def test_the_counts_behind_an_embedding_rate_are_not_compared() -> None:
    assert keys(changed(["dev", "retrieval", "hybrid", "recall_at_5"], rate(32, 34))) == []


def test_latency_is_never_compared() -> None:
    assert keys(changed(["dev", "latency", "why"], {"n": 9, "p50_ms": 4000})) == []


def test_a_key_the_fresh_run_lacks_is_a_difference() -> None:
    fresh = copy.deepcopy(BASE)
    del fresh["dev"]["sql"]
    assert "dev.sql.execution_accuracy.value" in keys(fresh)


def test_only_the_sections_that_ran_are_compared() -> None:
    fresh = copy.deepcopy(BASE)
    del fresh["heldout"]
    assert keys(fresh, ("dev", "shared")) == []


def test_the_report_is_written_with_sorted_keys_and_a_trailing_newline() -> None:
    text = report.dump({"b": 1, "a": {"d": 0.5, "c": [2, 1]}})
    assert text.endswith("}\n") and json.loads(text) == {"a": {"c": [2, 1], "d": 0.5}, "b": 1}
    assert text.index('"a"') < text.index('"b"') and text.index('"c"') < text.index('"d"')


def test_the_dashboard_headline_keeps_to_the_numbers_it_can_show() -> None:
    line = report.headline(BASE["dev"], BASE["shared"])
    assert line == {
        "sql": {"execution_accuracy": 0.8333},
        "retrieval": {"hybrid_recall_at_5": 0.9412},
        "hostile_sql": {"harmful": 0},
    }
