import os
from collections.abc import AsyncIterator, Callable, Iterator

import psycopg
import pytest
from psycopg.rows import TupleRow

from app import db
from app.config import ROOT
from app.identity import role_conninfo

Connection = psycopg.Connection[TupleRow]

SERVICE_PASSWORDS = {
    "postgres": "POSTGRES_PASSWORD",
    "app_writer": "APP_WRITER_PASSWORD",
    "gold_reader": "GOLD_READER_PASSWORD",
}


def _load_secrets() -> None:
    # Inside the test container compose passes these as env; on the host they come from the file.
    path = ROOT / "var" / "secrets.env"
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                os.environ.setdefault(key.strip(), value.strip())


_load_secrets()


def password_for(role: str) -> str:
    return os.environ[SERVICE_PASSWORDS.get(role, f"PGPASS_{role.upper()}")]


def connect(role: str, password: str | None = None) -> Connection:
    """Logs in as the role itself. Tests never SET ROLE from a superuser, which would test the wrong identity."""
    return psycopg.connect(role_conninfo(role, password or password_for(role)), autocommit=True, connect_timeout=3)


def _database_reachable() -> bool:
    try:
        with connect("postgres"):
            return True
    except (psycopg.OperationalError, KeyError):
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    integration = [item for item in items if item.get_closest_marker("integration")]
    if integration and not _database_reachable():
        skip = pytest.mark.skip(reason="compose database unreachable; run make up or docker compose up -d db init")
        for item in integration:
            item.add_marker(skip)


@pytest.fixture
def login() -> Iterator[Callable[..., Connection]]:
    opened: list[Connection] = []

    def _login(role: str, password: str | None = None) -> Connection:
        conn = connect(role, password)
        opened.append(conn)
        return conn

    yield _login
    for conn in opened:
        conn.close()


@pytest.fixture
def superuser(login: Callable[..., Connection]) -> Connection:
    return login("postgres")


@pytest.fixture(scope="module", autouse=True)
async def _close_pools() -> AsyncIterator[None]:
    # Per module, because each login role has a connection limit and the next module may log in as it directly.
    yield
    await db.close_all()
