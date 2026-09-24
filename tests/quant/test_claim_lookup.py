import pickle
from dataclasses import replace

import psycopg
import pytest
from psycopg.rows import TupleRow

from app.answer.lookup import LookupResult, answer_lookup
from app.identity import principal_for

pytestmark = pytest.mark.integration

Connection = psycopg.Connection[TupleRow]


def claim_in(superuser: Connection, region: str) -> int:
    row = superuser.execute("SELECT min(claim_id) FROM core.claims WHERE region = %s", (region,)).fetchone()
    assert row is not None
    return int(row[0])


def missing_claim(superuser: Connection) -> int:
    row = superuser.execute("SELECT max(claim_id) + 1000 FROM core.claims").fetchone()
    assert row is not None
    return int(row[0])


def without_id(result: LookupResult) -> bytes:
    marker = str(result.claim_id)
    return pickle.dumps(replace(result, claim_id=0, text=result.text.replace(marker, "<id>")))


async def test_a_missing_claim_and_a_hidden_claim_read_the_same(superuser: Connection) -> None:
    dana = principal_for("dana")
    hidden = await answer_lookup(dana, claim_in(superuser, "North"))
    missing = await answer_lookup(dana, missing_claim(superuser))
    assert hidden.text == f"I can't find claim {hidden.claim_id}."
    assert without_id(hidden) == without_id(missing)


async def test_an_adjuster_opens_a_claim_in_their_region(superuser: Connection) -> None:
    claim_id = claim_in(superuser, "West")
    result = await answer_lookup(principal_for("dana"), claim_id)
    assert result.kind == "found"
    assert result.text.startswith(f"Claim {claim_id} is ")
    assert result.fields["region"] == "West"


async def test_a_supervisor_opens_claims_anywhere(superuser: Connection) -> None:
    result = await answer_lookup(principal_for("priya"), claim_in(superuser, "North"))
    assert result.kind == "found"


async def test_analysts_cannot_open_claims(superuser: Connection) -> None:
    result = await answer_lookup(principal_for("sam"), claim_in(superuser, "West"))
    assert (result.kind, result.text) == (
        "not_allowed",
        "Analysts see aggregates only, so I can't open individual claims.",
    )
    assert result.sql is None and not result.fields
