import re
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from app.answer import drivers as dv
from app.answer import why
from app.answer.format import change_refs
from app.answer.types import Claim, Draft, Evidence
from app.identity import Principal, principal_for
from app.sandbox import templates
from app.sandbox.client import Table
from app.semantic.layer import Layer
from app.semantic.query import MetricQuery, Period
from app.verify import verify

HEADLINE = [{"period": "current", "value": Decimal("1000.00")}, {"period": "prior", "value": Decimal("400.00")}]
# Per free dimension: paid losses by group, then the claims-with-a-payment count the sum is split over.
PAID = {
    "peril": [
        ("current", "hail", "800"),
        ("current", "wind", "200"),
        ("prior", "hail", "150"),
        ("prior", "wind", "250"),
    ],
    "state": [("current", "CO", "700"), ("current", "AZ", "300"), ("prior", "CO", "100"), ("prior", "AZ", "300")],
}
COUNTS = {
    "peril": [("current", "hail", "8"), ("current", "wind", "2"), ("prior", "hail", "3"), ("prior", "wind", "2")],
    "state": [("current", "CO", "7"), ("current", "AZ", "3"), ("prior", "CO", "1"), ("prior", "AZ", "4")],
}


async def decompose_here(role: str, table: Table, params: dict[str, Any]) -> dict[str, Any]:
    return templates.decompose(pd.DataFrame(table.rows, columns=table.columns), params)


async def fetched(principal: Principal, mq: MetricQuery, layer: Layer) -> dv.Fetched:
    (dim,) = mq.group_by
    source = COUNTS if mq.measure == "claims_paid" else PAID
    rows = tuple((period, key, Decimal(value)) for period, key, value in source[dim])
    return dv.Fetched(("period", dim, "value"), rows, f"SELECT {mq.measure} BY {dim}", measure=mq.measure)


async def test_every_share_of_a_two_dimension_sum_split_recomputes_from_the_rows_it_names(
    monkeypatch: pytest.MonkeyPatch, layer: Layer
) -> None:
    # A sum split fetches a count per group as well, so each dimension adds more evidence rows than it indexes.
    split_by: list[list[dv.Group]] = []
    pick = dv.pick_drivers

    def every_group(per_dimension: list[list[dv.Group]]) -> list[tuple[dv.Group, ...]]:
        split_by.extend(per_dimension)
        return pick(per_dimension)

    monkeypatch.setattr(dv, "fetch", fetched)
    mq = MetricQuery("paid_losses", {"region": ("West",)}, period=Period.quarter(2025, 2))
    assert dv.free_dimensions(mq, layer, analyst=False) == ["peril", "state"]
    sides = dv.sides(dv.Fetched(("period", "value"), tuple(tuple(r.values()) for r in HEADLINE), ""), ratio=False)
    with monkeypatch.context() as patched:
        patched.setattr(dv, "pick_drivers", every_group)
        chosen, rows, results, _ = await why._split(
            principal_for("dana"), mq, layer, decompose_here, sides["current"], sides["prior"], len(HEADLINE)
        )
    groups = [group for per_dimension in split_by for group in per_dimension]
    assert [len(per_dimension) for per_dimension in split_by] == [2, 2] and len(rows) == 16 and chosen
    evidence = Evidence((*HEADLINE, *rows), (), tuple(results), ())
    change = change_refs(Decimal(1000), Decimal(400), "currency", (0, 1))
    claims = tuple(Claim(f"It accounts for {g.ref.display} of the rise.", (g.ref, *change), ()) for g in groups)
    checks = verify(Draft(claims, ()), evidence, principal_for("dana")).checks
    assert all(check.supported for check in checks), [(c.claim.text, c.reasons) for c in checks if not c.supported]
    # The state split's rows start after the peril split's value and count rows, and each share's numerator names
    # the paid-loss rows of its own group only, never the count rows beside them.
    for group in groups:
        named = {int(i) for i in re.findall(r"\[(\d+)\]", group.derivation)} - {0, 1}
        own = [(evidence.rows[i][group.dim], evidence.rows[i]["measure"]) for i in named]
        assert named and set(own) == {(group.key, "paid_losses")}, (group, named)
