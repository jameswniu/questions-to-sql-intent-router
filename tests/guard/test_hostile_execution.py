import os
import re
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from psycopg.rows import TupleRow

from app.config import ROOT
from app.identity import principals, role_conninfo

pytestmark = pytest.mark.integration

_DASHES = chr(45) * 2
_SEPARATOR = _DASHES + " next"
_COMMENT = _DASHES + " "
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_ALL_REGIONS = {"North", "South", "East", "West"}
_WATCH = ("pg_advisory", "pg_logical_emit_message")

Conn = psycopg.Connection[TupleRow]


def _hostile_statements() -> list[str]:
    text = (ROOT / "tests" / "data" / "hostile_sql.sql").read_text()
    out: list[str] = []
    for block in text.split(_SEPARATOR):
        lines = [ln for ln in block.splitlines() if not ln.lstrip().startswith(_COMMENT)]
        stmt = "\n".join(lines).strip()
        if stmt:
            out.append(stmt)
    return out


HOSTILE = _hostile_statements()
CHAT_ROLES = {p.db_role: set(p.regions) for p in principals().values()}


def _connect(role: str, password: str, *, autocommit: bool) -> Conn:
    return psycopg.connect(role_conninfo(role, password), autocommit=autocommit, connect_timeout=5)


def _password(role: str) -> str:
    return os.environ[f"PGPASS_{role.upper()}"]


def _scalar(conn: Conn, statement: str) -> Any:
    row = conn.execute(statement).fetchone()
    assert row is not None
    return row[0]


def _canary() -> tuple[int, int, tuple[tuple[str, str], ...], bool]:
    with _connect("postgres", os.environ["POSTGRES_PASSWORD"], autocommit=True) as conn:
        claims = int(_scalar(conn, "SELECT count(*) FROM core.claims"))
        payments = int(_scalar(conn, "SELECT count(*) FROM core.payments"))
        settings = tuple(sorted(conn.execute("SELECT key, value FROM app.settings").fetchall()))
        empty = "SELECT to_regclass('public.scratch') IS NULL AND to_regclass('scratch') IS NULL"
        scratch = bool(_scalar(conn, empty))
    return claims, payments, settings, scratch


def _harm(role: str, hidden: set[str], statement: str) -> tuple[set[str], bool]:
    flags: set[str] = set()
    executed = False
    conn = _connect(role, _password(role), autocommit=False)
    try:
        rows: list[tuple[Any, ...]] = []
        try:
            cur = conn.execute(statement)
            executed = True
            if cur.description:
                rows = cur.fetchall()
        except psycopg.Error:
            rows = []
        conn.rollback()

        who = conn.execute("SELECT current_user::text, session_user::text").fetchone()
        conn.rollback()
        if who != (role, role):
            flags.add("identity")

        timeout = conn.execute("SHOW statement_timeout").fetchone()
        conn.rollback()
        if timeout is None or timeout[0] != "4s":
            flags.add("timeout")

        for row in rows:
            for value in row:
                cell = str(value)
                if _SSN.search(cell):
                    flags.add("pii")
                if cell in hidden:
                    flags.add("hidden_region")
    finally:
        conn.close()
    return flags, executed


@dataclass(frozen=True)
class Tally:
    statements: int
    roles: int
    harmful: int
    state_unchanged: bool
    # Watched capabilities that still execute at the database layer, by statement.
    open_at_db: dict[str, set[str]]

    @property
    def executions(self) -> int:
        return self.statements * self.roles


def tally() -> Tally:
    """Every hostile statement run as every chat role; the eval runner reports the same tally."""
    before = _canary()
    harmful = 0
    executed_at_db: dict[str, set[str]] = {}
    for statement in HOSTILE:
        for role, regions in CHAT_ROLES.items():
            hidden = _ALL_REGIONS - regions
            flags, executed = _harm(role, hidden, statement)
            if flags:
                harmful += 1
                print(f"HARM {role}: {sorted(flags)} :: {statement[:60]}")
            if executed:
                executed_at_db.setdefault(statement, set()).add(role)
    after = _canary()
    watched = {s: roles for s, roles in executed_at_db.items() if any(w in s.lower() for w in _WATCH)}
    return Tally(len(HOSTILE), len(CHAT_ROLES), harmful, after == before, watched)


def test_no_hostile_statement_does_harm_as_any_chat_role() -> None:
    result = tally()
    print(f"\nhostile matrix: {result.statements} statements x {result.roles} roles = {result.executions} executions")
    print(f"harmful executions = {result.harmful}")

    if result.open_at_db:
        print("CAPABILITY STILL OPEN at the DB layer (blocked today only by check(), pending the PUBLIC revoke):")
        for statement, roles in result.open_at_db.items():
            print(f"  runs for {sorted(roles)}: {statement[:70]}")
    else:
        print("advisory locks and pg_logical_emit_message: already blocked at the DB layer for chat roles")

    assert result.state_unchanged
    assert result.harmful == 0
