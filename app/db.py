import asyncio
import os
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from app.identity import Principal, db_credentials, role_conninfo

Params = Sequence[Any] | Mapping[str, Any] | None
Statement = str | sql.SQL | sql.Composed
Pool = AsyncConnectionPool[AsyncConnection[TupleRow]]

# Headroom over the server-side statement_timeout, so the server normally ends a slow query first
# and the client deadline only fires when the server or the network stops answering.
CLIENT_GRACE_S = 0.5


@dataclass(frozen=True)
class Rows:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool
    ms: float
    sql: str


class QueryTimeout(Exception):
    pass


_chat_pools: dict[str, Pool] = {}
_writer: Pool | None = None
_lock = asyncio.Lock()


async def _read_only(conn: AsyncConnection[TupleRow]) -> None:
    await conn.set_read_only(True)


async def _chat_pool(principal: Principal) -> Pool:
    # One pool per login role and never shared, so a connection only ever carries the
    # identity it logged in with.
    async with _lock:
        pool = _chat_pools.get(principal.db_role)
        if pool is None:
            pool = Pool(
                db_credentials(principal),
                min_size=1,
                max_size=3,
                open=False,
                configure=_read_only,
                name=f"chat-{principal.db_role}",
            )
            await pool.open()
            _chat_pools[principal.db_role] = pool
        return pool


async def _writer_pool() -> Pool:
    global _writer
    async with _lock:
        if _writer is None:
            _writer = Pool(
                role_conninfo("app_writer", os.environ["APP_WRITER_PASSWORD"]),
                min_size=1,
                max_size=2,
                open=False,
                name="app-writer",
            )
            await _writer.open()
        return _writer


async def run(
    principal: Principal, query: Statement, params: Params = None, *, timeout_s: float = 4.0, row_cap: int = 500
) -> Rows:
    pool = await _chat_pool(principal)
    started = time.perf_counter()
    try:
        # Cancelling the task that awaits psycopg makes psycopg send a cancel request and wait for
        # the server to abandon the statement, so the connection returns to the pool idle.
        async with asyncio.timeout(timeout_s + CLIENT_GRACE_S):
            async with pool.connection() as conn, conn.transaction():
                await conn.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(int(timeout_s * 1000)))
                # A server-side cursor, so a huge result is read only as far as the cap. It also
                # refuses anything that cannot back a cursor, which is every statement but a query.
                async with conn.cursor(name="answer") as cur:
                    await cur.execute(query, params)
                    columns = [column.name for column in cur.description or ()]
                    fetched = await cur.fetchmany(row_cap + 1)
                text = _as_text(query, conn)
    except (TimeoutError, psycopg.errors.QueryCanceled) as exc:
        raise QueryTimeout(f"query ran past {timeout_s}s as {principal.db_role}") from exc
    return Rows(
        columns=columns,
        rows=fetched[:row_cap],
        truncated=len(fetched) > row_cap,
        ms=(time.perf_counter() - started) * 1000,
        sql=text,
    )


def _as_text(query: Statement, conn: AsyncConnection[TupleRow]) -> str:
    return query if isinstance(query, str) else query.as_string(conn)


async def write(query: Statement, params: Params = None) -> None:
    pool = await _writer_pool()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(query, params)


@contextmanager
def gold_connection() -> Iterator[psycopg.Connection[TupleRow]]:
    """Bypasses row-level security. Only the eval runner uses it, to compute gold answers."""
    with psycopg.connect(role_conninfo("gold_reader", os.environ["GOLD_READER_PASSWORD"])) as conn:
        conn.read_only = True
        yield conn


async def close_all() -> None:
    global _writer
    async with _lock:
        pools = [*_chat_pools.values(), *([_writer] if _writer else [])]
        _chat_pools.clear()
        _writer = None
    for pool in pools:
        await pool.close()
