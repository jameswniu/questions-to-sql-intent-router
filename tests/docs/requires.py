import shutil

import pytest

from app.config import settings

needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed here")


def _models_present() -> bool:
    path = settings().embed_model_path
    return path is not None and path.exists()


needs_models = pytest.mark.skipif(
    not _models_present(), reason="embedding models are baked into the app image; run make test"
)
