import json
from typing import Any

import pytest

from app import db
from app.config import ROOT
from app.identity import Principal
from app.semantic.layer import Layer, default_layer

CASES = ROOT / "evals" / "cases"


def dev_cases(name: str) -> list[dict[str, Any]]:
    cases = (json.loads(line) for line in (CASES / name).read_text().splitlines() if line.strip())
    return [case for case in cases if case["split"] == "dev"]


@pytest.fixture(scope="session")
def layer() -> Layer:
    return default_layer()


async def visible(principal: Principal, chunk_ids: list[str]) -> set[str]:
    """The chunk ids among these that the principal can read, asked through their own login."""
    found = await db.run(principal, "SELECT chunk_id FROM rag.chunks WHERE chunk_id = ANY(%s)", (chunk_ids,))
    return {row[0] for row in found.rows}
