from decimal import Decimal
from typing import get_args

import pytest

from app.answer.figures import Form, figures
from app.answer.format import NumberRef
from app.verify import NORMALIZATION, matches


def cites(text: str, value: str) -> bool:
    (written,) = figures(text)
    return matches(written, NumberRef(Decimal(value), text, "value", 0, "value"))


def test_every_written_form_has_one_rule_and_so_do_multiples() -> None:
    assert set(NORMALIZATION) == {*get_args(Form), "multiple"}


@pytest.mark.parametrize(
    ("form", "text", "near", "far"),
    [
        ("cents", "$4,108,452.79", "4108452.80", "4108452.77"),
        ("dollars", "$4,108,453", "4108452.50", "4108452.49"),
        ("scaled", "$4.1 million", "4149999", "4150001"),
        ("scaled", "4.1M", "4050000", "4049999"),
        ("scaled", "$412K", "412499", "412501"),
        ("percent", "18.8%", "0.18798", "0.1886"),
        ("percent", "18.8%", "18.84", "18.86"),
        ("percent", "19%", "0.1851", "0.1849"),
        ("percent", "16.3 points", "0.163485", "0.1636"),
        ("decimal", "0.81", "0.8149", "0.8151"),
        ("integer", "1,001", "1001", "1002"),
    ],
)
def test_a_figure_cites_a_value_within_its_display_rounding_and_no_further(
    form: str, text: str, near: str, far: str
) -> None:
    assert figures(text)[0].form == form
    assert cites(text, near)
    assert not cites(text, far)


def test_a_percent_cites_the_fraction_or_the_percent() -> None:
    assert cites("12.5%", "0.125") and cites("12.5%", "12.5")
    assert not cites("12.5%", "0.135")


def test_whole_dollars_are_the_workflows_own_display_of_a_value_with_cents() -> None:
    assert cites("$4,108,453", "4108452.79")


def test_a_change_is_written_as_a_size_so_its_figure_cites_either_sign() -> None:
    # The sign is traced through the direction word beside the figure instead; see test_comparisons.
    assert cites("$11,422,990", "-11422989.97")


def test_a_written_minus_needs_a_negative_value() -> None:
    assert cites("-$1,234", "-1234")
    assert not cites("-$1,234", "1234")


def test_an_integer_count_is_exact() -> None:
    assert not cites("569", "569.4")
