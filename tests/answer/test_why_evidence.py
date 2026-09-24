import json
from dataclasses import replace
from functools import partial
from typing import Any

import pandas as pd
import pytest

from app import events as ev
from app import handle
from app.answer import why
from app.identity import principal_for
from app.sandbox import templates
from app.sandbox.client import Table
from app.sources.documents import Hit
from app.verify import evidence_values, names_in, verify
from app.web.stream import encode, fields_of

pytestmark = pytest.mark.integration

WEST = "Why were paid losses in the West so high in Q2 2025?"  # why-002, asked by Dana
Q2, Q1 = ["2025-04-01", "2025-06-30"], ["2025-01-01", "2025-03-31"]


async def decompose_here(role: str, table: Table, params: dict[str, Any]) -> dict[str, Any]:
    return templates.decompose(pd.DataFrame(table.rows, columns=table.columns), params)


async def no_documents(*_: Any, **__: Any) -> list[Hit]:
    return []


async def explained(monkeypatch: pytest.MonkeyPatch, user: str, question: str) -> handle.Handled:
    """What the why route hands the pipeline, with the change split here instead of in sandboxd, and no documents
    searched, since that needs the embedding models."""
    monkeypatch.setattr(handle, "answer_why", partial(why.answer_why, decompose=decompose_here))
    monkeypatch.setattr(why, "search", no_documents)
    return await handle.why(principal_for(user), question, None)


def statements(handled: handle.Handled) -> list[dict[str, Any]]:
    """Each SQL evidence event's payload as the browser receives it."""
    sent = [event for event in handled.events if isinstance(event, ev.Evidence) and event.kind == "sql"]
    return [json.loads(str(encode("evidence", fields_of(event) or {}).raw_data))["payload"] for event in sent]


def placeholders(sql: str) -> int:
    """psycopg marks each bound value with %s and writes a literal percent sign as %%."""
    return sql.replace("%%", "").count("%s")


@pytest.mark.parametrize(
    ("user", "question"), [("dana", WEST), ("sam", "Why were paid losses so high in Q2 2025?")], ids=["rows", "agg"]
)
async def test_every_statement_behind_a_why_answer_shows_the_values_bound_to_it(
    monkeypatch: pytest.MonkeyPatch, user: str, question: str
) -> None:
    sent = statements(await explained(monkeypatch, user, question))
    assert len(sent) > 1, "the change was not split"
    for statement in sent:
        assert statement["params"] and len(statement["params"]) == placeholders(statement["sql"]), statement


async def test_a_why_answer_sends_its_bound_values_as_a_figure_does(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dates go as ISO days and a filter's values as a list, for the figure and the explanation alike.
    [figure] = statements(await handle.quantitative(principal_for("dana"), "Paid losses in the West in Q2 2025", None))
    assert figure["params"] == [*Q2, ["West"]]
    headline = statements(await explained(monkeypatch, "dana", WEST))[0]
    assert headline["params"] == [*Q2, *Q2, *Q1, ["West"]]


async def test_every_row_behind_a_why_answer_names_the_measure_it_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    handled = await explained(monkeypatch, "dana", WEST)
    assert handled.evidence is not None and handled.draft is not None
    rows = handled.evidence.rows
    # Paid losses are split over the claims with a payment, and both come back in the value column.
    assert {row["measure"] for row in rows} == {"paid_losses", "claims_paid"}
    # The verifier reads a measure as neither a figure nor a name, and every sentence still checks out.
    bare = replace(handled.evidence, rows=tuple({k: v for k, v in row.items() if k != "measure"} for row in rows))
    assert evidence_values(handled.evidence) == evidence_values(bare)
    assert names_in(handled.evidence) == names_in(bare)
    checks = verify(handled.draft, handled.evidence, principal_for("dana")).checks
    assert all(check.supported for check in checks), [check.reasons for check in checks if not check.supported]
