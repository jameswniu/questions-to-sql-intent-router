from decimal import Decimal

import pytest

from app.answer.format import Format, change_refs, format_change, format_value, ref


@pytest.mark.parametrize(
    ("value", "fmt", "shown"),
    [
        (Decimal("4108452.79"), "currency", "$4,108,453"),
        (Decimal("0.50"), "currency", "$1"),
        (Decimal("-1234.4"), "currency", "-$1,234"),
        (335, "integer", "335"),
        (1234567, "integer", "1,234,567"),
        (Decimal("0.18798449"), "percent", "18.8%"),
        (Decimal("0.0005"), "percent", "0.1%"),
        (0.915, "percent", "91.5%"),
    ],
)
def test_values_format_by_the_measure_format(value: Decimal | int | float, fmt: Format, shown: str) -> None:
    assert format_value(value, fmt) == shown


def test_a_change_in_a_rate_is_in_points() -> None:
    assert format_change(Decimal("0.1635"), "percent") == "16.4 points"
    assert format_change(Decimal("-0.01"), "percent") == "1.0 point"


def test_change_refs_record_how_each_figure_was_derived() -> None:
    change, pct = change_refs(Decimal("150"), Decimal("100"), "currency", (0, 1))
    assert (change.display, change.derivation) == ("$50", "value[0] - value[1]")
    assert (pct.display, pct.derivation) == ("50.0%", "(value[0] - value[1]) / value[1]")


def test_change_refs_name_the_columns_an_aggregate_ratio_came_from() -> None:
    change, pct = change_refs(Decimal("0.2"), Decimal("0.1"), "percent", (0, 1), "num / den")
    assert change.derivation == "(num / den)[0] - (num / den)[1]"
    assert pct.derivation == "((num / den)[0] - (num / den)[1]) / (num / den)[1]"


def test_no_percent_change_from_zero() -> None:
    assert len(change_refs(Decimal("5"), Decimal("0"), "integer", (0, 1))) == 1


def test_a_reference_keeps_the_exact_value_behind_the_rounded_display() -> None:
    figure = ref(Decimal("12754.744808"), "currency", "value", 3, "value")
    assert (figure.value, figure.display, figure.row_index) == (Decimal("12754.744808"), "$12,755", 3)
