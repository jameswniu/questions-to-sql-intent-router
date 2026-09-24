"""Scores the verifier on the planted-error set: python -m evals.verifier.score"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.identity import principal_for
from app.verify import verify
from evals.verifier.planted import Case, load


@dataclass(frozen=True)
class Score:
    planted: int
    caught: int
    clean: int
    false_alarms: int
    collateral: int  # claims cut in a mutated draft that carried no planted error
    by_mutation: dict[str, tuple[int, int]] = field(default_factory=dict)  # kind of error: (caught, planted)

    @property
    def recall(self) -> float:
        return self.caught / self.planted if self.planted else 0.0

    @property
    def false_alarm_rate(self) -> float:
        return self.false_alarms / self.clean if self.clean else 0.0


def score(cases: Iterable[Case]) -> Score:
    """A planted error is caught when every claim carrying it is cut; a clean draft alarms when any claim is cut."""
    planted = caught = clean = false_alarms = collateral = 0
    by_mutation: dict[str, tuple[int, int]] = {}
    for case in cases:
        checks = verify(case.draft, case.evidence, principal_for(case.user)).checks
        cut = {i for i, check in enumerate(checks) if not check.supported}
        if not case.planted:
            clean += 1
            false_alarms += bool(cut)
            continue
        hit = set(case.planted) <= cut
        planted += 1
        caught += hit
        collateral += len(cut - set(case.planted))
        was = by_mutation.get(case.mutation, (0, 0))
        by_mutation[case.mutation] = (was[0] + hit, was[1] + 1)
    return Score(planted, caught, clean, false_alarms, collateral, dict(sorted(by_mutation.items())))


def report(result: Score) -> str:
    lines = [
        f"caught {result.caught} of {result.planted} planted errors (recall {result.recall:.2f})",
        f"cut a claim in {result.false_alarms} of {result.clean} clean drafts (false-alarm rate "
        f"{result.false_alarm_rate:.2f})",
        f"cut {result.collateral} claims beside a planted error",
    ]
    lines += [f"  {mutation}: {hit} of {total}" for mutation, (hit, total) in result.by_mutation.items()]
    return "\n".join(lines)


if __name__ == "__main__":
    print(report(score(load())))
