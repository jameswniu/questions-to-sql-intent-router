from datetime import date
from typing import Any

import pytest

from app.config import load_yaml
from app.seed.rows import CHANNELS
from app.semantic.layer import Layer, LayerError, parse_layer


def test_real_layer_loads_with_its_examples(layer: Layer) -> None:
    assert set(layer.measures) == {
        "claim_count",
        "paid_losses",
        "claims_paid",
        "avg_severity",
        "denial_rate",
        "loss_ratio",
        "open_reserve",
    }
    assert len(layer.examples) == 6


def test_a_synonym_that_names_two_measures_is_rejected(layer_doc: dict[str, Any]) -> None:
    layer_doc["measures"]["paid_losses"]["synonyms"].append("claims")
    with pytest.raises(LayerError, match="'claims' maps to both"):
        parse_layer(layer_doc)


def test_a_synonym_shared_by_a_measure_and_a_value_is_rejected(layer_doc: dict[str, Any]) -> None:
    layer_doc["dimensions"]["peril"]["synonyms"]["payouts"] = "water"
    with pytest.raises(LayerError, match="maps to both"):
        parse_layer(layer_doc)


def test_an_unknown_source_view_is_rejected(layer_doc: dict[str, Any]) -> None:
    layer_doc["measures"]["claim_count"]["source"] = "core.claims"
    with pytest.raises(LayerError, match="unknown source view"):
        parse_layer(layer_doc)


def test_an_expression_naming_a_missing_column_is_rejected(layer_doc: dict[str, Any]) -> None:
    layer_doc["measures"]["paid_losses"]["sql"] = "sum(reserve_amount)"
    with pytest.raises(LayerError, match="reserve_amount"):
        parse_layer(layer_doc)


@pytest.mark.parametrize(
    ("query", "problem"),
    [
        ({"measure": "paid_lossez", "period": "2025"}, "unknown measure"),
        ({"measure": "paid_losses", "filters": {"county": ["Dane"]}, "period": "2025"}, "unknown dimension county"),
        ({"measure": "paid_losses", "filters": {"peril": ["flood"]}, "period": "2025"}, "unknown peril values"),
        ({"measure": "paid_losses", "group_by": ["adjuster"], "period": "2025"}, "unknown dimension adjuster"),
        ({"measure": "paid_losses", "period": "2025-Q5"}, "not a period"),
    ],
)
def test_an_example_with_unknown_names_is_rejected(
    layer_doc: dict[str, Any], query: dict[str, Any], problem: str
) -> None:
    layer_doc["examples"].append({"q": "made up", "query": query})
    with pytest.raises(LayerError, match=problem):
        parse_layer(layer_doc)


def test_a_dimension_synonym_pointing_at_no_value_is_rejected(layer_doc: dict[str, Any]) -> None:
    layer_doc["dimensions"]["channel"]["synonyms"]["fax"] = "fax"
    with pytest.raises(LayerError, match="unknown values"):
        parse_layer(layer_doc)


def test_open_reserve_is_a_snapshot_with_no_date_or_time_split(layer: Layer) -> None:
    reserve = layer.measures["open_reserve"]
    assert reserve.point_in_time and reserve.dates == ()
    assert not {"month", "quarter", "year"} & set(layer.dimensions_for(reserve))


def test_loss_ratio_splits_only_by_region_and_time(layer: Layer) -> None:
    ratio = layer.measures["loss_ratio"]
    assert ratio.dates == ("paid_date", "month")
    assert layer.dimensions_for(ratio) == ("region", "month", "quarter", "year")


def test_payment_measures_reach_claim_columns_through_the_claim(layer: Layer) -> None:
    assert "channel" in layer.dimensions_for(layer.measures["paid_losses"])


def test_a_measure_cannot_be_split_by_a_column_its_own_filter_fixes(layer: Layer) -> None:
    for name in ("avg_severity", "denial_rate", "open_reserve"):
        assert "status" not in layer.dimensions_for(layer.measures[name])


def test_analysts_reach_neither_status_nor_snapshot_measures(layer: Layer) -> None:
    assert "status" not in layer.dimensions_for(layer.measures["claim_count"], analyst=True)
    assert layer.dimensions_for(layer.measures["open_reserve"], analyst=True) == ()


def test_channel_values_are_the_four_the_seed_writes(layer: Layer) -> None:
    assert set(layer.dimensions["channel"].values) == {channel for channel, _ in CHANNELS}
    assert layer.dimensions["channel"].synonyms["online"] == "web"
    assert layer.dimensions["channel"].synonyms["mobile app"] == "app"


def test_longer_phrases_are_tried_first(layer: Layer) -> None:
    lengths = [len(p.text) for p in layer.phrases]
    assert lengths == sorted(lengths, reverse=True)


def test_semantic_claims_yaml_and_data_policy_yaml_agree_on_as_of_and_coverage(layer_doc: dict[str, Any]) -> None:
    # The seed writes its rows inside data/policy.yaml's window, and the figures path resolves periods against
    # semantic/claims.yaml's, so a date changed in one file alone answers questions about rows that aren't there.
    def window(doc: dict[str, Any]) -> dict[str, date]:
        coverage = doc["coverage"]
        return {"as_of": doc["as_of"], "coverage.start": coverage["start"], "coverage.end": coverage["end"]}

    assert window(layer_doc) == window(load_yaml("policy.yaml")), (
        "semantic/claims.yaml and data/policy.yaml must carry the same as_of and coverage window"
    )
