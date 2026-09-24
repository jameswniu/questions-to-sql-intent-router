from app.memory import LastTurn, Memory
from app.route import Turn
from app.semantic.query import MetricQuery, Period

Q2 = MetricQuery("paid_losses", {"region": ("West",)}, period=Period.quarter(2025, 2))


def test_a_session_remembers_its_last_turn() -> None:
    memory = Memory()
    memory.put("dana", "s1", LastTurn("quantitative", "Paid losses in the West in Q2 2025", Q2))
    memory.put("dana", "s1", LastTurn("lookup", "show me claim 100013", claim_id=100013))
    assert memory.get("dana", "s1") == LastTurn("lookup", "show me claim 100013", claim_id=100013)
    assert memory.get("dana", "s2") is None


def test_the_router_sees_the_turn_it_knows() -> None:
    assert LastTurn("quantitative", "Paid losses in Q2 2025", Q2).turn == Turn("quantitative", "Paid losses in Q2 2025")


def test_another_user_in_the_same_session_starts_clean() -> None:
    memory = Memory()
    memory.put("dana", "shared", LastTurn("quantitative", "Paid losses in the West in Q2 2025", Q2))
    assert memory.get("june", "shared") is None
    # The takeover dropped dana's turn too, so switching back does not bring it along.
    assert memory.get("dana", "shared") is None


def test_writing_as_another_user_replaces_the_session_owner() -> None:
    memory = Memory()
    memory.put("dana", "shared", LastTurn("quantitative", "Paid losses in Q2 2025", Q2))
    memory.put("june", "shared", LastTurn("qualitative", "Is flood damage covered?"))
    assert memory.get("june", "shared") == LastTurn("qualitative", "Is flood damage covered?")
    assert len(memory) == 1


def test_clear_forgets_only_that_users_session() -> None:
    memory = Memory()
    memory.put("dana", "s1", LastTurn("qualitative", "Is flood damage covered?"))
    memory.put("dana", "s2", LastTurn("qualitative", "Is earthquake damage covered?"))
    memory.clear("june", "s1")
    assert memory.get("dana", "s1") is not None
    memory.clear("dana", "s1")
    assert memory.get("dana", "s1") is None and memory.get("dana", "s2") is not None


def test_the_least_recently_used_session_goes_first_past_capacity() -> None:
    memory = Memory(capacity=2)
    memory.put("dana", "a", LastTurn("qualitative", "one"))
    memory.put("dana", "b", LastTurn("qualitative", "two"))
    memory.get("dana", "a")
    memory.put("dana", "c", LastTurn("qualitative", "three"))
    assert memory.get("dana", "b") is None
    assert memory.get("dana", "a") is not None and memory.get("dana", "c") is not None
