import re
from decimal import Decimal

import psycopg
import pytest
from psycopg.rows import TupleRow

from app import db
from app.answer.quant import HIDDEN_LINE, QuantResult, answer_quant, verify_sql
from app.identity import principal_for
from app.semantic.compile import compile
from app.semantic.layer import Layer
from app.semantic.query import MetricQuery, Period
from app.semantic.resolve import resolve

pytestmark = pytest.mark.integration

Connection = psycopg.Connection[TupleRow]
MEASURES = ["claim_count", "paid_losses", "claims_paid", "avg_severity", "denial_rate", "loss_ratio", "open_reserve"]


async def ask(user: str, question: str, layer: Layer) -> QuantResult:
    return await answer_quant(principal_for(user), question, None, layer=layer)


@pytest.mark.parametrize("user", ["dana", "priya", "sam"])
@pytest.mark.parametrize("name", MEASURES)
async def test_every_measure_runs_as_each_kind_of_user(layer: Layer, user: str, name: str) -> None:
    principal = principal_for(user)
    measure = layer.measures[name]
    if principal.kind == "analyst" and not measure.analyst:
        pytest.skip("snapshot measures are not aggregates; the refusal has its own test")
    period = None if measure.point_in_time else Period.year(2025)
    query = resolve(MetricQuery(name, group_by=("region",), period=period), layer, analyst=principal.kind == "analyst")
    assert isinstance(query, MetricQuery)
    compiled = compile(query, layer, principal)
    sql = verify_sql(compiled.sql, relations=compiled.relations)
    found = await db.run(principal, sql, compiled.params)
    assert tuple(found.columns) == compiled.columns
    assert found.rows


async def test_analysts_are_told_snapshots_are_not_aggregates(layer: Layer) -> None:
    result = await ask("sam", "What are our open reserves?", layer)
    assert result.kind == "not_allowed" and result.sql is None


async def test_a_grouped_average_is_a_ratio_of_sums(layer: Layer) -> None:
    grouped = await ask("priya", "Average severity by peril in 2025", layer)
    total = await ask("priya", "Average severity in 2025", layer)
    cols = grouped.columns
    num = sum((Decimal(row[cols.index("numerator")]) for row in grouped.rows), Decimal(0))
    den = sum((Decimal(row[cols.index("denominator")]) for row in grouped.rows), Decimal(0))
    overall = total.cells[0].value
    assert overall is not None
    assert abs(overall - num / den) < Decimal("0.000001")
    means = [cell.value for cell in grouped.cells if cell.value is not None]
    assert abs(sum(means, Decimal(0)) / len(means) - overall) > 1


async def test_an_adjuster_asking_outside_their_regions_is_told_what_they_can_see(
    layer: Layer, superuser: Connection
) -> None:
    result = await ask("dana", "How many claims were reported in the North in 2025?", layer)
    hidden = superuser.execute(
        "SELECT count(*) FROM core.claims WHERE region = 'North' AND loss_date BETWEEN '2025-01-01' AND '2025-12-31'"
    ).fetchone()
    assert hidden is not None
    assert result.kind == "not_allowed"
    assert result.text == "You can see claims in the West only, so I can't report on the North."
    assert str(hidden[0]) not in result.text and f"{hidden[0]:,}" not in result.text
    assert result.sql is None and not result.numbers


async def test_an_adjuster_asking_about_a_state_elsewhere_gets_the_same_answer_shape(layer: Layer) -> None:
    result = await ask("dana", "Paid losses in Texas in 2025", layer)
    assert (result.kind, result.text) == (
        "not_allowed",
        "You can see claims in the West only, so I can't report on Texas.",
    )


async def test_a_partly_visible_question_answers_the_visible_part_and_says_so(layer: Layer) -> None:
    both = await ask("dana", "Paid losses in Colorado and Texas in 2025", layer)
    colorado = await ask("dana", "Paid losses in Colorado in 2025", layer)
    assert both.cells[0].value == colorado.cells[0].value
    assert both.text.endswith("You can see claims in the West only, so this leaves out the South.")


def unreferenced_digits(result: QuantResult) -> str:
    text = result.text
    for display in sorted((ref.display for ref in result.numbers), key=len, reverse=True):
        text = text.replace(display, "#")
    for pattern in (r"\b(?:19|20)\d{2}\b", r"\bQ[1-4]\b", r"\b[A-Z][a-z]+ \d{1,2},"):
        text = re.sub(pattern, "", text)
    return "".join(re.findall(r"\d", text))


@pytest.mark.parametrize(
    ("user", "question"),
    [
        ("dana", "How much did we pay on hail claims in Colorado in Q2 2025?"),
        ("priya", "Denial rate for wind claims in 2025 versus 2024"),
        ("priya", "Paid losses by region in 2025 compared to 2024"),
        ("priya", "Paid losses by month in 2026"),
        ("priya", "What are our open reserves?"),
        ("sam", "Claim count by state and peril in May 2025"),
        ("sam", "Denial rate in 2025 vs 2024"),
    ],
)
async def test_every_number_in_an_answer_has_a_reference(layer: Layer, user: str, question: str) -> None:
    result = await ask(user, question, layer)
    assert result.kind == "answer"
    assert unreferenced_digits(result) == ""
    for ref in result.numbers:
        assert ref.display in result.text


async def test_analyst_answers_withhold_small_groups_and_say_why(layer: Layer) -> None:
    result = await ask("sam", "Claim count by state and peril in May 2025", layer)
    hidden = [cell for cell in result.cells if cell.hidden]
    assert hidden and all(cell.value is None for cell in hidden)
    assert result.text.count(": withheld") == len(hidden)
    assert HIDDEN_LINE in result.text
    assert {ref.row_index for ref in result.numbers}.isdisjoint(cell.row_index for cell in hidden)


async def test_a_snapshot_answer_names_the_data_date(layer: Layer) -> None:
    result = await ask("priya", "What are our open reserves?", layer)
    assert "as of July 15, 2026" in result.text


async def test_answers_expose_what_ran(layer: Layer) -> None:
    result = await ask("priya", "Claim count by region in 2025", layer)
    assert result.query is not None and result.sql is not None and result.rows
    assert set(result.provenance) == set(result.columns)
    assert str(result.data_as_of) == "2026-07-15"
