import os
from collections.abc import Sequence
from functools import cache
from pathlib import Path

from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder

from app.config import settings

# fastembed knows the models by these names; the weights are the pinned snapshots baked into the image.
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_REPO = "Qdrant/bge-small-en-v1.5-onnx-Q"
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
DIMENSIONS = 384
# onnxruntime's default of one thread per core ran slower here than eight, measured in the app container.
THREADS = min(8, os.cpu_count() or 1)


class ModelsMissing(RuntimeError):
    pass


def _path(value: Path | None, var: str) -> Path:
    if value is None or not value.exists():
        raise ModelsMissing(f"{var} is not set to a model directory; the app image sets it to /opt/models")
    return value


def revision(path: Path) -> str:
    """The Hugging Face commit the snapshot was downloaded at, as recorded next to the files."""
    for metadata in sorted((path / ".cache" / "huggingface" / "download").rglob("*.metadata")):
        return metadata.read_text().splitlines()[0]
    return "unknown"


@cache
def model_id() -> str:
    return f"{EMBED_REPO}@{revision(_path(settings().embed_model_path, 'EMBED_MODEL_PATH'))}"


@cache
def _embedder() -> TextEmbedding:
    path = _path(settings().embed_model_path, "EMBED_MODEL_PATH")
    return TextEmbedding(EMBED_MODEL, specific_model_path=str(path), threads=THREADS)


@cache
def _reranker() -> TextCrossEncoder:
    path = _path(settings().rerank_model_path, "RERANK_MODEL_PATH")
    return TextCrossEncoder(RERANK_MODEL, specific_model_path=str(path), threads=THREADS)


def passages(texts: Sequence[str]) -> list[list[float]]:
    # Batches pad to their longest text, so embedding in length order wastes far less work.
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    vectors = _embedder().embed([texts[i] for i in order], batch_size=32)
    out: list[list[float]] = [[] for _ in texts]
    for i, vector in zip(order, vectors, strict=True):
        out[i] = vector.tolist()
    return out


def query(text: str) -> list[float]:
    vector: list[float] = next(iter(_embedder().query_embed(text))).tolist()
    return vector


def rerank(text: str, candidates: Sequence[str]) -> list[float]:
    return [float(score) for score in _reranker().rerank(text, list(candidates), batch_size=32)]


def literal(vector: Sequence[float]) -> str:
    """pgvector's text form, with enough digits to round-trip the model's float32 output exactly."""
    return "[" + ",".join(f"{x:.9g}" for x in vector) + "]"
