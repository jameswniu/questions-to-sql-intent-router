from dataclasses import dataclass
from typing import Any, NamedTuple

from app import events as ev
from app.answer.types import Answer
from app.identity import principal_for
from app.replay import Logged, ask_logged
from evals.metrics import wilson
from evals.splits import Case


class Verdict(NamedTuple):
    answered: bool
    right: bool


def rate(hits: int, n: int) -> dict[str, float | int]:
    return wilson(hits, n).as_dict()


@dataclass(frozen=True)
class Outcome:
    case: Case
    user: str
    logged: Logged

    @property
    def done(self) -> ev.Done | None:
        return next((e for e in reversed(self.logged.events) if isinstance(e, ev.Done)), None)

    @property
    def route(self) -> str:
        return self.done.route if self.done else "error"

    @property
    def outcome(self) -> str:
        return self.done.outcome if self.done else "error"

    @property
    def label(self) -> str:
        """The route as the routing labels name it: a refusal is the route refuse."""
        return "refuse" if self.outcome == "refused" else self.route

    @property
    def answer(self) -> Answer | None:
        return next((e for e in self.logged.events if isinstance(e, Answer)), None)

    @property
    def answered(self) -> bool:
        """An answer the asker was shown. One whose every claim the verifier cut says it could not confirm them,
        which is an abstention."""
        answer = self.answer
        if self.outcome != "answer" or answer is None:
            return False
        return bool(answer.claims_kept) or not answer.claims_cut

    def evidence(self, kind: str) -> list[Any]:
        return [e.payload for e in self.logged.events if isinstance(e, ev.Evidence) and e.kind == kind]


async def run_case(case: Case, user: str | None = None) -> Outcome:
    asker = user or case["user"]
    return Outcome(case, asker, await ask_logged(principal_for(asker), case["q"], source="eval"))
