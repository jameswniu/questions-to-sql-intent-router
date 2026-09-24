# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc AS uv

FROM python:3.13.13-slim-trixie@sha256:aa938a849bcb82dce8f49480f056ab82bf5c1c3ebc294f0430f37b6820e7f286 AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr=5.5.0-1+b1* tesseract-ocr-eng=1:4.1.0-2 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
# OCR numbers are only reproducible with the same language data, so a changed eng.traineddata fails the build.
RUN echo "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2  /usr/share/tesseract-ocr/5/tessdata/eng.traineddata" | sha256sum -c -

# The project itself is never installed: the code runs from /srv, which is the working directory.
FROM base AS deps
COPY --from=uv /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

FROM deps AS deps-dev
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project

# Models are fetched at a pinned revision during the build and loaded from a local path at run
# time, so a running container never reaches Hugging Face.
FROM deps AS models
ARG EMBED_REPO=Qdrant/bge-small-en-v1.5-onnx-Q
ARG EMBED_REVISION=52398278842ec682c6f32300af41344b1c0b0bb2
ARG RERANK_REPO=Xenova/ms-marco-MiniLM-L-6-v2
ARG RERANK_REVISION=a09144355adeed5f58c8ed011d209bf8ee5a1fec
RUN python - <<'PY'
import os
from huggingface_hub import snapshot_download

tokenizer_files = ["config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.txt"]
for repo, revision, target, weights in [
    (os.environ["EMBED_REPO"], os.environ["EMBED_REVISION"], "/opt/models/bge-small-en-v1.5", "model_optimized.onnx"),
    (os.environ["RERANK_REPO"], os.environ["RERANK_REVISION"], "/opt/models/ms-marco-MiniLM-L-6-v2", "onnx/model.onnx"),
]:
    snapshot_download(repo, revision=revision, local_dir=target, allow_patterns=[weights, *tokenizer_files])
PY
RUN HF_HUB_OFFLINE=1 python - <<'PY'
from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder

embedder = TextEmbedding("BAAI/bge-small-en-v1.5", specific_model_path="/opt/models/bge-small-en-v1.5")
assert len(next(iter(embedder.embed(["wind and hail deductible"])))) == 384
reranker = TextCrossEncoder("Xenova/ms-marco-MiniLM-L-6-v2", specific_model_path="/opt/models/ms-marco-MiniLM-L-6-v2")
scores = list(reranker.rerank("hail deductible", ["the hail deductible is 2% of Coverage A", "payments outage"]))
assert scores[0] > scores[1], scores
PY

FROM base AS app-base
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home-dir /srv app
ENV EMBED_MODEL_PATH=/opt/models/bge-small-en-v1.5 \
    RERANK_MODEL_PATH=/opt/models/ms-marco-MiniLM-L-6-v2 \
    FASTEMBED_CACHE_PATH=/opt/models \
    HF_HUB_OFFLINE=1
COPY --from=models /opt/models /opt/models
WORKDIR /srv
COPY app ./app
COPY data ./data
COPY db ./db
COPY semantic ./semantic

FROM app-base AS test
COPY --from=deps-dev /opt/venv /opt/venv
USER app

FROM app-base AS runtime
COPY --from=deps /opt/venv /opt/venv
USER app
EXPOSE 8000
CMD ["uvicorn", "app.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
