import os
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any, Literal, cast

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_DIR = ROOT / "db"

Backend = Literal["none", "anthropic", "vertex"]


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    as_of: date
    backend: Backend
    embed_model_path: Path | None
    rerank_model_path: Path | None


def load_yaml(name: str) -> dict[str, Any]:
    with (DATA_DIR / name).open() as fh:
        return cast(dict[str, Any], yaml.safe_load(fh))


@cache
def policy() -> dict[str, Any]:
    return load_yaml("policy.yaml")


@cache
def events() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], load_yaml("events.yaml")["events"])


@cache
def settings() -> Settings:
    backend = os.environ.get("CLAUDE_BACKEND") or "none"
    if backend not in ("none", "anthropic", "vertex"):
        raise ValueError(f"CLAUDE_BACKEND must be none, anthropic or vertex, not {backend!r}")
    as_of = os.environ.get("AS_OF")
    return Settings(
        db_host=os.environ.get("PGHOST", "127.0.0.1"),
        db_port=int(os.environ.get("PGPORT", "55432")),
        db_name=os.environ.get("PGDATABASE", "claims"),
        as_of=date.fromisoformat(as_of) if as_of else policy()["as_of"],
        backend=cast(Backend, backend),
        embed_model_path=_optional_path("EMBED_MODEL_PATH"),
        rerank_model_path=_optional_path("RERANK_MODEL_PATH"),
    )


def _optional_path(var: str) -> Path | None:
    value = os.environ.get(var)
    return Path(value) if value else None
