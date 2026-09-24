import copy
from typing import Any

import pytest
import yaml

from app.semantic.layer import LAYER_PATH, Layer, default_layer


@pytest.fixture(scope="session")
def layer() -> Layer:
    return default_layer()


@pytest.fixture
def layer_doc() -> dict[str, Any]:
    with LAYER_PATH.open() as fh:
        return copy.deepcopy(yaml.safe_load(fh))
