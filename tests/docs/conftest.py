import json
from typing import Any

import pytest

from app.config import policy
from app.seed import notes as note_seed
from app.seed import rows
from app.seed.notes import ClaimFacts, Note
from app.seed.rows import Dataset
from app.seed.scans import TRUTH_PATH


@pytest.fixture(scope="session")
def dataset() -> Dataset:
    return rows.generate(as_of=policy()["as_of"])


@pytest.fixture(scope="session")
def claim_facts(dataset: Dataset) -> dict[int, ClaimFacts]:
    return {c.claim_id: c for c in note_seed.claims_from_dataset(dataset)}


@pytest.fixture(scope="session")
def notes(claim_facts: dict[int, ClaimFacts]) -> list[Note]:
    return note_seed.generate(list(claim_facts.values()))


@pytest.fixture(scope="session")
def truth() -> list[dict[str, Any]]:
    return [json.loads(line) for line in TRUTH_PATH.read_text().splitlines()]
