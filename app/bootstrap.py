import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow

from app import replay
from app.config import DB_DIR
from app.identity import principals, role_conninfo
from app.ingest import steps
from app.seed import rows

log = logging.getLogger("bootstrap")

Connection = psycopg.Connection[TupleRow]

# Roles outside users.yaml whose passwords also come from var/secrets.env.
SERVICE_ROLES = {"app_writer": "APP_WRITER_PASSWORD", "gold_reader": "GOLD_READER_PASSWORD"}


@dataclass(frozen=True)
class Step:
    name: str
    run: Callable[[Connection], str]
    # A once step is skipped after it has succeeded; the others are idempotent and run every time.
    once: bool


def apply_schema(conn: Connection) -> str:
    files = sorted(DB_DIR.glob("*.sql"))
    for path in files:
        conn.execute(path.read_text().encode())
    return ", ".join(path.name for path in files)


def sync_principals(conn: Connection) -> str:
    known = list(principals().values())
    for p in known:
        conn.execute(
            "INSERT INTO app.principals (role_name, kind, regions) VALUES (%s, %s, %s)"
            " ON CONFLICT (role_name) DO UPDATE SET kind = excluded.kind, regions = excluded.regions",
            (p.db_role, p.kind, list(p.regions)),
        )
    conn.execute("DELETE FROM app.principals WHERE NOT role_name = ANY (%s)", ([p.db_role for p in known],))
    return f"{len(known)} principals"


def set_passwords(conn: Connection) -> str:
    env_vars = {p.db_role: f"PGPASS_{p.db_role.upper()}" for p in principals().values()} | SERVICE_ROLES
    for role, var in env_vars.items():
        password = os.environ.get(var)
        if not password:
            raise RuntimeError(f"{var} is not set; run make secrets")
        # Hashed on this side, so the plain password never travels to the server or its logs.
        verifier = conn.pgconn.encrypt_password(password.encode(), role.encode(), b"scram-sha-256").decode()
        conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(verifier)))
    return f"{len(env_vars)} roles"


def seed_rows(conn: Connection) -> str:
    counts = rows.load(conn, rows.generate())
    return ", ".join(f"{table} {count}" for table, count in counts.items())


# Later phases append their steps (document ingest, eval replay) to this list.
STEPS = [
    Step("schema", apply_schema, once=True),
    Step("principals", sync_principals, once=False),
    Step("passwords", set_passwords, once=False),
    Step("seed", seed_rows, once=True),
    Step("render_documents", steps.render_documents, once=False),
    Step("render_notes", steps.render_notes, once=False),
    Step("ingest", steps.ingest, once=False),
    Step("replay", replay.replay, once=True),
]


def connect(timeout_s: float = 60.0) -> Connection:
    conninfo = role_conninfo("postgres", os.environ["POSTGRES_PASSWORD"])
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return psycopg.connect(conninfo, connect_timeout=3)
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def is_done(conn: Connection, step: str) -> bool:
    if conn.execute("SELECT to_regclass('app.bootstrap_state')").fetchone() == (None,):
        return False
    return conn.execute("SELECT 1 FROM app.bootstrap_state WHERE step = %s", (step,)).fetchone() is not None


def run(conn: Connection, steps: list[Step]) -> None:
    for step in steps:
        started = time.perf_counter()
        with conn.transaction():
            if step.once and is_done(conn, step.name):
                log.info("%-10s skipped, already done", step.name)
                continue
            detail = step.run(conn)
            conn.execute(
                "INSERT INTO app.bootstrap_state (step) VALUES (%s) ON CONFLICT (step) DO UPDATE SET done_at = now()",
                (step.name,),
            )
        log.info("%-10s %6.0f ms  %s", step.name, (time.perf_counter() - started) * 1000, detail)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    with connect() as conn:
        run(conn, STEPS)


if __name__ == "__main__":
    main()
