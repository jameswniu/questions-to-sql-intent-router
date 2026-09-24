import pytest

from app import events as ev
from app import handle
from app.identity import principal_for
from app.semantic.layer import default_layer

COVERED = default_layer().coverage.label

# Every way a question ends outside the data: a figure or a why question about a year before it, a why question
# whose period has nothing before it to compare with, a forecast for a year after it, and one that names no year.
OUTSIDE = [
    (handle.quantitative, "How many claims did we get in 2022?"),
    (handle.why, "Why were paid losses so high in 2023?"),
    (handle.why, "Why did claims go up in January 2024?"),
    (handle.out_of_data, "Forecast paid losses for 2027"),
    (handle.out_of_data, "Will we see more hail claims?"),
]


@pytest.mark.parametrize(("handler", "question"), OUTSIDE, ids=[question for _, question in OUTSIDE])
async def test_an_out_of_data_reply_names_the_range_the_data_covers_once(
    handler: handle.Handler, question: str
) -> None:
    handled = await handler(principal_for("priya"), question, None)
    [event] = handled.events
    assert handled.outcome == "out_of_data" and isinstance(event, ev.OutOfData)
    assert event.covered == COVERED
    assert event.message.count(COVERED) == 1, event.message


async def test_a_reply_that_only_says_where_the_data_starts_ends_with_the_range() -> None:
    handled = await handle.why(principal_for("priya"), "Why did claims go up in January 2024?", None)
    [event] = handled.events
    assert isinstance(event, ev.OutOfData)
    assert event.message == (
        "The data starts on January 1, 2024, so there is nothing before January 2024 to compare it with."
        " My data covers January 2024 to June 2026."
    )
