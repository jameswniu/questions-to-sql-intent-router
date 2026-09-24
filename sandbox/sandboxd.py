# Launches one throwaway container per analysis job. This process holds the Docker socket, which
# is root on the host: it is the one component that must never run model output itself, and it
# never does. It only passes the job to a container through stdin.
#
# Run on a host with Docker:  python -m sandbox.sandboxd   (from the repo root)
from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import IO, Any

from app.sandbox.codecheck import check

IMAGE = os.environ.get("SANDBOX_IMAGE", "claims-qa-sandbox:1")
WALL_CLOCK_S = 10.0
# The image runs every job under coreutils timeout, set this far past the wall clock, so a container
# ends itself even when the daemon's kill never lands.
DEADLINE_GRACE_S = 1.0
# One budget for stdout and stderr together; the runner sends a job's own prints to stderr.
OUTPUT_CAP_BYTES = 1 << 20
MAX_REQUEST_BYTES = 8 << 20
# Bounds each read and write on a client connection, so a slow client holds its thread this long at most.
REQUEST_TIMEOUT_S = 10.0
# Connections past this are refused on the accepting thread, before anything is read from them.
MAX_CONNECTIONS = int(os.environ.get("SANDBOXD_MAX_CONNECTIONS", "32"))
PER_PRINCIPAL_JOBS = 2
# Without a global cap, many principals at 512m each could still exhaust the host.
MAX_JOBS = int(os.environ.get("SANDBOXD_MAX_JOBS", "8"))
# Must match app/sandbox/templates.py TEMPLATES; a test holds the two together.
TEMPLATE_NAMES = frozenset({"yoy", "decompose", "zscore", "slope"})
NAME_PREFIX = "claims-qa-sbx-"
PRINCIPAL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,62}$")
STDERR_KEEP_BYTES = 4096
# How long launch keeps killing and asking docker before it answers and leaves the container to the reaper.
# Short, so the reply still lands inside the client's timeout.
STOP_WITHIN_S = 1.5
STOP_POLL_S = 0.2
CLI_GRACE_S = 2.0
REAP_EVERY_S = 2.0
KILL_MESSAGES = {
    "timeout": f"killed: the {WALL_CLOCK_S:g} s wall clock ran out",
    "oom": "killed: the job exceeded its 512m memory limit",
    "output_cap": f"killed: output passed the {OUTPUT_CAP_BYTES} byte cap",
}
CLI_ENV_KEYS = (
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
)

# Every container that may still be running, until docker confirms it is not.
_live: set[str] = set()
_live_lock = threading.Lock()


def docker_argv(name: str, image: str = IMAGE, wall_clock_s: float = WALL_CLOCK_S) -> list[str]:
    return [
        "docker", "run", "-i", "--rm", "--pull", "never", "--name", name,
        "--network", "none",
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "--memory", "512m", "--memory-swap", "512m",
        "--cpus", "1", "--pids-limit", "64",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", "65534:65534",
        "--ulimit", "nofile=64:64",
        # Output reaches the daemon through the attached pipes; a log driver would also copy it to host disk.
        "--log-driver", "none",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        image,
        # The image's entrypoint is timeout --signal=KILL; these are its seconds and command.
        f"{wall_clock_s + DEADLINE_GRACE_S:g}", "python", "-I", "/runner/runner.py",
    ]  # fmt: skip


def _cli_env() -> dict[str, str]:
    # The docker CLI gets only what it needs to find the engine; no -e flag above copies from it anyway.
    return {k: v for k in CLI_ENV_KEYS if (v := os.environ.get(k))}


def _docker(*args: str, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, env=_cli_env(), check=False
    )


def _kill(name: str) -> None:
    # The exit status is not trusted either way; _running is what decides whether the container is gone.
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        _docker("kill", name)


def _running(name: str) -> bool | None:
    """Whether docker reports the container running, or None when docker could not be asked."""
    try:
        probe = _docker("inspect", "--type", "container", "--format", "{{.State.Running}}", name)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if probe.returncode == 0:
        return probe.stdout.strip() == "true"
    if "No such container" in probe.stderr:
        return False  # --rm has already removed it
    return None


def _stop(name: str, *, kill: bool, within_s: float) -> bool:
    """Kill until docker reports the container stopped; False if that is still unconfirmed after within_s."""
    deadline = time.monotonic() + within_s
    while True:
        if kill:
            _kill(name)
        if _running(name) is False:
            return True
        if time.monotonic() >= deadline:
            return False
        kill = True
        time.sleep(STOP_POLL_S)


def _forget(name: str) -> None:
    with _live_lock:
        _live.discard(name)


class Reaper:
    """Keeps killing containers whose end docker has not confirmed. Each holds its admission slot until then."""

    def __init__(self, every_s: float = REAP_EVERY_S) -> None:
        self.every_s = every_s
        self._pending: dict[str, Callable[[], None]] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def watch(self, name: str, on_gone: Callable[[], None]) -> None:
        with self._lock:
            self._pending[name] = on_gone
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="sandboxd-reaper", daemon=True)
                self._thread.start()

    def pending(self) -> set[str]:
        with self._lock:
            return set(self._pending)

    def sweep(self) -> None:
        for name in self.pending():
            if not _stop(name, kill=True, within_s=0):
                continue
            with self._lock:
                on_gone = self._pending.pop(name, None)
            if on_gone is not None:
                _forget(name)
                on_gone()

    def _loop(self) -> None:
        while True:
            time.sleep(self.every_s)
            try:
                self.sweep()
            except Exception as exc:  # noqa: BLE001 - the reaper must outlive any one bad sweep
                sys.stderr.write(f"sandboxd reaper: {exc!r}\n")


REAPER = Reaper()


def _parse_reply(out: bytes) -> dict[str, Any] | None:
    lines = out.decode("utf-8", "replace").strip().splitlines()
    if len(lines) != 1:
        return None
    try:
        reply = json.loads(lines[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
        return None
    if reply["ok"]:
        return {"ok": True, "result": reply.get("result")}
    return {"ok": False, "error": str(reply.get("error"))}


def _nothing() -> None:
    pass


def launch(
    job: dict[str, Any],
    *,
    image: str = IMAGE,
    wall_clock_s: float = WALL_CLOCK_S,
    output_cap: int = OUTPUT_CAP_BYTES,
    on_gone: Callable[[], None] = _nothing,
) -> dict[str, Any]:
    """Run one job in a fresh container and return {ok, result|error, killed, ms, exit}.

    on_gone is called exactly once, when docker confirms the container has stopped. That can be after
    this returns: a container docker cannot confirm stays tracked, and the reaper keeps trying.
    """
    name = f"{NAME_PREFIX}{uuid.uuid4().hex[:12]}"
    payload = json.dumps({k: job.get(k) for k in ("template", "code", "params", "table")}).encode()
    out, err = bytearray(), bytearray()
    received = 0
    budget = threading.Lock()
    capped = threading.Event()

    with _live_lock:
        _live.add(name)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            docker_argv(name, image, wall_clock_s),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_cli_env(),
        )
    except BaseException:
        _forget(name)
        on_gone()
        raise

    gone = False
    try:
        assert proc.stdin and proc.stdout and proc.stderr
        stdin = proc.stdin

        def feed() -> None:
            try:
                stdin.write(payload)
                stdin.close()
            except OSError:
                pass

        def drain(stream: IO[bytes], keep: bytearray, keep_limit: int) -> None:
            nonlocal received
            while chunk := os.read(stream.fileno(), 65536):
                with budget:
                    received += len(chunk)
                    first_over = received > output_cap and not capped.is_set()
                    if first_over:
                        capped.set()
                if first_over:
                    # From its own thread: docker kill waits on the container, which waits on these pipes.
                    threading.Thread(target=_kill, args=(name,), daemon=True).start()
                if capped.is_set():
                    continue  # keep draining so the CLI never blocks on a full pipe
                if len(keep) < keep_limit:
                    keep.extend(chunk[: keep_limit - len(keep)])

        workers = [
            threading.Thread(target=feed, daemon=True),
            threading.Thread(target=drain, args=(proc.stdout, out, output_cap), daemon=True),
            threading.Thread(target=drain, args=(proc.stderr, err, STDERR_KEEP_BYTES), daemon=True),
        ]
        for w in workers:
            w.start()

        timed_out = False
        try:
            # --cpus limits the rate, not the time; this wall clock is what ends a busy loop.
            exit_code: int | None = proc.wait(timeout=wall_clock_s)
        except subprocess.TimeoutExpired:
            timed_out, exit_code = True, None
        # The CLI also exits when it loses the engine, so its exit alone does not prove the container stopped.
        gone = _stop(name, kill=timed_out, within_s=STOP_WITHIN_S)
        try:
            cli_exit = proc.wait(timeout=CLI_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()  # the CLI only; a container still running is the reaper's
            cli_exit = proc.wait()
        for w in workers:
            w.join(timeout=5)
        ms = round((time.monotonic() - started) * 1000)
    finally:
        if gone:
            _forget(name)
            on_gone()
        else:
            REAPER.watch(name, on_gone)

    killed: str | None = None
    if capped.is_set():
        killed = "output_cap"
    elif timed_out:
        killed = "timeout"
    elif exit_code == 137:
        # SIGKILL that we did not send comes from the kernel OOM killer. --rm usually removes the
        # container before inspect can confirm it, so the exit status is the evidence.
        killed = "oom"

    reported_exit = cli_exit if cli_exit >= 0 else None  # negative: we killed the CLI, not the job
    reply: dict[str, Any]
    if killed:
        reply = {"ok": False, "error": KILL_MESSAGES[killed]}
    else:
        reply = _parse_reply(bytes(out)) or {
            "ok": False,
            "error": f"the runner exited with status {reported_exit} and no result: "
            + err.decode("utf-8", "replace").strip()[-500:],
        }
    return {**reply, "killed": killed, "ms": ms, "exit": reported_exit}


class Slots:
    def __init__(self, per_principal: int = PER_PRINCIPAL_JOBS, total: int = MAX_JOBS) -> None:
        self.per_principal, self.total = per_principal, total
        self._active: dict[str, int] = {}
        self._lock = threading.Lock()

    def acquire(self, principal: str) -> HTTPStatus | None:
        with self._lock:
            if self._active.get(principal, 0) >= self.per_principal:
                return HTTPStatus.TOO_MANY_REQUESTS
            if sum(self._active.values()) >= self.total:
                return HTTPStatus.SERVICE_UNAVAILABLE
            self._active[principal] = self._active.get(principal, 0) + 1
            return None

    def release(self, principal: str) -> None:
        with self._lock:
            left = self._active[principal] - 1
            if left:
                self._active[principal] = left
            else:
                del self._active[principal]


def validate(body: Any) -> str | None:
    if not isinstance(body, dict):
        return "the body must be a JSON object"
    principal = body.get("principal")
    if not isinstance(principal, str) or not PRINCIPAL_RE.match(principal):
        return "principal must be a login role name"
    template, code = body.get("template"), body.get("code")
    if (template is None) == (code is None):
        return "give exactly one of template or code"
    if template is not None and template not in TEMPLATE_NAMES:
        return f"unknown template {template!r}"
    if code is not None:
        if not isinstance(code, str):
            return "code must be a string"
        verdict = check(code)
        if not verdict.ok:
            return f"code rejected: {verdict.reason}"
    if not isinstance(body.get("params", {}), dict):
        return "params must be an object"
    table = body.get("table")
    if not isinstance(table, dict):
        return "table must be an object with columns and rows"
    columns, rows = table.get("columns"), table.get("rows")
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        return "table.columns must be a list of strings"
    if not isinstance(rows, list) or not all(isinstance(r, list) and len(r) == len(columns) for r in rows):
        return "table.rows must be lists as long as table.columns"
    return None


def content_length(values: list[str]) -> int | tuple[HTTPStatus, str]:
    """The number of body bytes to read, or the status and reason to refuse the request with."""
    if len(values) != 1:
        return HTTPStatus.BAD_REQUEST, "exactly one Content-Length header is required"
    value = values[0]
    # isascii too, because str.isdigit accepts digits such as "²" that int() does not.
    if not (value.isascii() and value.isdigit()):
        return HTTPStatus.BAD_REQUEST, "Content-Length must be a non-negative integer"
    if len(value) > len(str(MAX_REQUEST_BYTES)) or int(value) > MAX_REQUEST_BYTES:
        return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large"
    return int(value)


def _raw_reply(status: HTTPStatus, body: dict[str, Any]) -> bytes:
    data = json.dumps(body).encode()
    head = (
        f"HTTP/1.1 {status.value} {status.phrase}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n"
    )
    return head.encode() + data


BUSY_REPLY = _raw_reply(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "too many connections"})


class SandboxServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        launcher: Callable[..., dict[str, Any]] = launch,
        *,
        request_timeout_s: float = REQUEST_TIMEOUT_S,
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        super().__init__(address, Handler)
        # Called as launcher(body, on_gone=...), and must call on_gone once the job's container is gone.
        self.launcher = launcher
        self.slots = Slots()
        self.request_timeout_s = request_timeout_s
        self._connections = threading.BoundedSemaphore(max_connections)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._connections.acquire(blocking=False):
            with contextlib.suppress(OSError):
                request.settimeout(1.0)
                request.sendall(BUSY_REPLY)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()


class Handler(BaseHTTPRequestHandler):
    """Serves one request per connection, so an idle client cannot hold a thread between requests."""

    server: SandboxServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        timeout_s = self.server.request_timeout_s
        # The socket timeout ends a stalled read or write. A client that trickles bytes resets it on
        # every one, so the whole request also has a deadline: past it, reads see end of file.
        self.connection.settimeout(timeout_s)
        self.expired = threading.Event()
        self.deadline = threading.Timer(timeout_s, self._stop_reading)
        self.deadline.daemon = True
        self.deadline.start()

    def _stop_reading(self) -> None:
        self.expired.set()
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RD)

    def finish(self) -> None:
        self.deadline.cancel()
        super().finish()

    def _send(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.deadline.cancel()
        if self.path == "/healthz":
            self._send(HTTPStatus.OK, {"ok": True, "docker_socket": os.path.exists("/var/run/docker.sock")})
        else:
            self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/run":
            self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Transfer-Encoding is not supported"})
            return
        length = content_length(self.headers.get_all("Content-Length") or [])
        if isinstance(length, tuple):
            status, error = length
            self._send(status, {"ok": False, "error": error})
            return
        try:
            raw = self.rfile.read(length)
        except TimeoutError:
            raw = b""
            self.expired.set()
        self.deadline.cancel()
        if len(raw) < length:
            if self.expired.is_set():
                self._send(
                    HTTPStatus.REQUEST_TIMEOUT, {"ok": False, "error": "the request body did not arrive in time"}
                )
            else:
                self._send(
                    HTTPStatus.BAD_REQUEST, {"ok": False, "error": "the body is shorter than its Content-Length"}
                )
            return
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "the body is not JSON"})
            return
        problem = validate(body)
        if problem:
            status = HTTPStatus.UNPROCESSABLE_ENTITY if problem.startswith("code rejected") else HTTPStatus.BAD_REQUEST
            self._send(status, {"ok": False, "error": problem})
            return

        principal = body["principal"]
        refused = self.server.slots.acquire(principal)
        if refused:
            why = (
                "concurrent jobs for this principal" if refused == HTTPStatus.TOO_MANY_REQUESTS else "jobs on this host"
            )
            self._send(refused, {"ok": False, "error": f"too many {why}", "killed": None, "ms": 0, "exit": None})
            return
        # The slot is released when the container is confirmed gone, which can be after this reply.
        result = self.server.launcher(body, on_gone=functools.partial(self.server.slots.release, principal))
        kind = "template:" + body["template"] if body.get("template") else "code"
        sys.stderr.write(
            json.dumps(
                {"principal": principal, "job": kind, **{k: result.get(k) for k in ("ok", "killed", "ms", "exit")}}
            )
            + "\n"
        )
        self._send(HTTPStatus.OK, result)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature is fixed by the base class
        pass


def kill_orphans() -> None:
    # A job container that outlives the daemon ends at its own deadline; this ends it now.
    try:
        listing = _docker("ps", "-q", "--filter", f"name=^{NAME_PREFIX}", timeout=10)
    except subprocess.TimeoutExpired:
        return
    for cid in listing.stdout.split():
        _kill(cid)


def main() -> None:
    host = os.environ.get("SANDBOXD_HOST", "0.0.0.0")
    port = int(os.environ.get("SANDBOXD_PORT", "8700"))
    kill_orphans()
    server = SandboxServer((host, port))

    def stop(signum: int, frame: Any) -> None:
        with _live_lock:
            names = list(_live)
        for name in names:
            _kill(name)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    sys.stderr.write(f"sandboxd listening on {host}:{port}, image {IMAGE}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
