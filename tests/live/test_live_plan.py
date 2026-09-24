from typing import Any

import pytest

from app.answer import why as no_key
from app.identity import principal_for
from app.live.why import plan
from app.semantic.layer import default_layer

CASES = [
    ("dana", "Why were paid losses in the West so high in Q2 2025?"),
    ("june", "Why were paid losses in the West so high in Q2 2025?"),
    ("sam", "Why were paid losses in the West so high in Q2 2025?"),
    ("priya", "Why did claims go up in January 2024?"),
    ("priya", "Why did paid losses go up?"),
    ("priya", "Why are open reserves so high?"),
    ("priya", "Why did the denial rate for wind claims rise in 2025?"),
    ("tomas", "Why did hail claims in Texas jump in Q2 2025?"),
]


class Explained(Exception):
    pass


async def _explained(*args: Any, **kwargs: Any) -> Any:
    raise Explained


@pytest.mark.parametrize(("user", "question"), CASES)
async def test_the_live_path_explains_exactly_the_questions_the_no_key_workflow_would(
    monkeypatch: pytest.MonkeyPatch, user: str, question: str
) -> None:
    """The live path makes its own checks before it spends a model call. They must stop the same questions the
    no-key workflow stops before it explains anything: clarifying, out of data, a snapshot, or out of scope."""
    monkeypatch.setattr(no_key, "_explain", _explained)
    principal, layer = principal_for(user), default_layer()
    try:
        await no_key.answer_why(principal, question, layer=layer)
        explains = False
    except Explained:
        explains = True
    assert (plan(principal, question, None, layer) is not None) == explains
