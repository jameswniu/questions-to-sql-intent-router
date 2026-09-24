from typing import Any

from evals.outcome import rate
from evals.verifier.planted import load
from evals.verifier.score import score
from tests.guard.test_hostile_execution import tally


def score_verifier() -> dict[str, Any]:
    """The planted-error set: a planted error is caught when every claim carrying it is cut."""
    result = score(load())
    return {
        "planted": result.planted,
        "caught": result.caught,
        "recall": rate(result.caught, result.planted),
        "clean": result.clean,
        "false_alarms": rate(result.false_alarms, result.clean),
        "collateral": result.collateral,
        "by_mutation": {mutation: rate(hit, total) for mutation, (hit, total) in result.by_mutation.items()},
    }


def score_hostile() -> dict[str, Any]:
    result = tally()
    return {
        "statements": result.statements,
        "roles": result.roles,
        "executions": result.executions,
        "harmful": result.harmful,
        "state_unchanged": result.state_unchanged,
    }
