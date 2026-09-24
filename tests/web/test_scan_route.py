import httpx
import psycopg
import pytest
from psycopg.rows import TupleRow

from tests.web.fakes import ClientFor

pytestmark = pytest.mark.integration


def _scan(superuser: psycopg.Connection[TupleRow], where: str) -> str:
    row = superuser.execute(
        f"SELECT doc_id FROM rag.documents WHERE kind = 'scan' AND {where} ORDER BY doc_id LIMIT 1"
    ).fetchone()
    assert row is not None, f"no scan where {where}"
    return str(row[0])


async def test_another_regions_scan_gets_the_same_404_as_a_missing_one(
    client: httpx.AsyncClient, superuser: psycopg.Connection[TupleRow]
) -> None:
    own, other = _scan(superuser, "region = 'West'"), _scan(superuser, "region <> 'West'")

    allowed = await client.get(f"/evidence/scan/{own}")
    assert allowed.status_code == 200
    assert allowed.content.startswith(b"\x89PNG")
    assert allowed.headers["cache-control"] == "private, no-store"

    forbidden = await client.get(f"/evidence/scan/{other}")
    missing = await client.get("/evidence/scan/scan-000000-invoice")
    assert forbidden.status_code == missing.status_code == 404
    assert forbidden.content == missing.content == b'{"detail":"Not found"}'
    assert forbidden.headers["content-type"] == missing.headers["content-type"]


async def test_an_analyst_cannot_open_any_claim_scan(
    client_for: ClientFor, superuser: psycopg.Connection[TupleRow]
) -> None:
    own = _scan(superuser, "region = 'West'")
    async with client_for("sam") as analyst:
        response = await analyst.get(f"/evidence/scan/{own}")
    assert response.status_code == 404
