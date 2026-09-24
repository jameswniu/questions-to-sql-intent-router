from collections import OrderedDict
from dataclasses import dataclass

from app.route import Turn
from app.semantic.query import MetricQuery

CAPACITY = 10_000


@dataclass(frozen=True)
class LastTurn:
    route: str
    question: str
    query: MetricQuery | None = None
    claim_id: int | None = None

    @property
    def turn(self) -> Turn:
        return Turn(self.route, self.question)


class Memory:
    """The last turn of each user's session, so a follow-up such as "and in Q3?" can build on it.

    A session belongs to the user who last wrote to it. When another user takes it over, what the first one asked
    is dropped, so it never shapes the new user's questions. This lives in the process and forgets the least
    recently used sessions past its capacity; production would keep it in the session store, shared by every replica.
    """

    def __init__(self, capacity: int = CAPACITY) -> None:
        self._turns: OrderedDict[tuple[str, str], LastTurn] = OrderedDict()
        self._owners: dict[str, str] = {}
        self._capacity = capacity

    def get(self, user_id: str, session_id: str) -> LastTurn | None:
        if self._owners.get(session_id, user_id) != user_id:
            self._forget(session_id)
            return None
        key = (user_id, session_id)
        turn = self._turns.get(key)
        if turn is not None:
            self._turns.move_to_end(key)
        return turn

    def put(self, user_id: str, session_id: str, turn: LastTurn) -> None:
        if self._owners.get(session_id, user_id) != user_id:
            self._forget(session_id)
        self._owners[session_id] = user_id
        self._turns[(user_id, session_id)] = turn
        self._turns.move_to_end((user_id, session_id))
        while len(self._turns) > self._capacity:
            (_, oldest), _ = self._turns.popitem(last=False)
            self._owners.pop(oldest, None)

    def clear(self, user_id: str, session_id: str) -> None:
        if self._owners.get(session_id) == user_id:
            self._forget(session_id)

    def _forget(self, session_id: str) -> None:
        owner = self._owners.pop(session_id, None)
        if owner is not None:
            self._turns.pop((owner, session_id), None)

    def __len__(self) -> int:
        return len(self._turns)
