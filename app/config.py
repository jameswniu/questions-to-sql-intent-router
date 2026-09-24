import os
from collections.abc import Mapping
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
# What LLM_BACKEND may be set to, and the backend each names. Unset means off.
BACKENDS: dict[str, Backend] = {"off": "none", "anthropic": "anthropic", "vertex": "vertex"}


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


def backend_from(env: Mapping[str, str]) -> Backend:
    """The model backend LLM_BACKEND names: none when it is unset or off, otherwise anthropic or vertex."""
    name = (env.get("LLM_BACKEND") or "off").strip().lower()
    if name not in BACKENDS:
        raise ValueError(f"LLM_BACKEND must be one of {', '.join(BACKENDS)}, not {name!r}")
    return BACKENDS[name]


@cache
def settings() -> Settings:
    as_of = os.environ.get("AS_OF")
    return Settings(
        db_host=os.environ.get("PGHOST", "127.0.0.1"),
        db_port=int(os.environ.get("PGPORT", "55432")),
        db_name=os.environ.get("PGDATABASE", "claims"),
        as_of=date.fromisoformat(as_of) if as_of else policy()["as_of"],
        backend=backend_from(os.environ),
        embed_model_path=_optional_path("EMBED_MODEL_PATH"),
        rerank_model_path=_optional_path("RERANK_MODEL_PATH"),
    )


def _optional_path(var: str) -> Path | None:
    value = os.environ.get(var)
    return Path(value) if value else None
