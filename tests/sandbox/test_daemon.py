from __future__ import annotations

import functools
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest

from app.sandbox.templates import TEMPLATES
from sandbox import sandboxd

JOB: dict[str, Any] = {
    "principal": "adjuster_mn",
    "template": "yoy",
    "code": None,
    "params": {},
    "table": {"columns": [], "rows": []},
}


def _post(url: str, body: Any, path: str = "/run") -> tuple[int, dict[str, Any]]:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(url + path, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_daemon_knows_exactly_the_templates_the_runner_has() -> None:
    assert set(TEMPLATES) == sandboxd.TEMPLATE_NAMES


def test_launch_flags_are_the_documented_limits_and_nothing_is_mounted() -> None:
    argv = sandboxd.docker_argv("claims-qa-sbx-test", "claims-qa-sandbox:1")
    assert argv == [
        "docker", "run", "-i", "--rm", "--pull", "never", "--name", "claims-qa-sbx-test",
        "--network", "none",
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "--memory", "512m", "--memory-swap", "512m",
        "--cpus", "1", "--pids-limit", "64",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", "65534:65534",
        "--ulimit", "nofile=64:64",
        "--log-driver", "none",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "claims-qa-sandbox:1",
        "11", "python", "-I", "/runner/runner.py",
    ]  # fmt: skip
    assert not {"-v", "--volume", "--mount", "--env-file", "--privileged"} & set(argv)
    # The in-container deadline trails whatever wall clock the daemon runs the job under.
    assert sandboxd.docker_argv("claims-qa-sbx-test", "claims-qa-sandbox:1", 2.5)[-4] == "3.5"


def test_healthz_answers_200(daemon_url: str) -> None:
    with urllib.request.urlopen(daemon_url + "/healthz", timeout=5) as resp:
        assert resp.status == 200 and json.loads(resp.read())["ok"] is True


def test_code_that_fails_codecheck_is_refused_with_422_before_any_container_starts(daemon_url: str) -> None:
    status, body = _post(
        daemon_url, {**JOB, "template": None, "code": "import os\ndef run(df, params):\n    return {}"}
    )
    assert status == 422 and "import" in body["error"]


def test_malformed_jobs_are_refused_with_400(daemon_url: str) -> None:
    cases = [
        b"not json",
        {**JOB, "principal": "x'); drop table claims;"},
        {**JOB, "template": "rm_rf"},
        {**JOB, "code": "def run(df, params):\n    return {}"},  # both template and code
        {**JOB, "template": None},  # neither
        {**JOB, "table": {"columns": ["a"], "rows": [[1, 2]]}},
        {**JOB, "params": ["not", "an", "object"]},
    ]
    for body in cases:
        status, reply = _post(daemon_url, body)
        assert status == 400, (body, reply)
        assert reply["ok"] is False


def test_unknown_paths_are_404(daemon_url: str) -> None:
    assert _post(daemon_url, JOB, path="/containers/create")[0] == 404


def test_slots_cap_each_principal_and_the_host() -> None:
    slots = sandboxd.Slots(per_principal=2, total=3)
    assert slots.acquire("a") is None and slots.acquire("a") is None
    assert slots.acquire("a") == 429
    assert slots.acquire("b") is None
    assert slots.acquire("c") == 503
    slots.release("a")
    assert slots.acquire("a") is None


def _raw(url: str, request: bytes, *, wait_s: float = 5.0) -> tuple[int, dict[str, Any]]:
    """Sends bytes as they are and reads until the daemon closes, which it does after every reply."""
    host, port = url.removeprefix("http://").split(":")
    received = b""
    with socket.create_connection((host, int(port)), timeout=wait_s) as conn:
        conn.sendall(request)
        try:
            while chunk := conn.recv(65536):
                received += chunk
        except ConnectionResetError:
            pass  # a close with unread input resets; the reply has already arrived by then
    head, _, body = received.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


def _post_head(headers: str) -> bytes:
    return f"POST /run HTTP/1.1\r\nHost: sandboxd\r\nContent-Type: application/json\r\n{headers}\r\n".encode()


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ("Content-Length: -1\r\n", 400),
        ("Content-Length: twelve\r\n", 400),
        ("Content-Length: 1e3\r\n", 400),
        ("", 400),
        ("Content-Length: 5\r\nContent-Length: 6\r\n", 400),
        ("Transfer-Encoding: chunked\r\n", 400),
        (f"Content-Length: {sandboxd.MAX_REQUEST_BYTES + 1}\r\n", 413),
        ("Content-Length: 99999999999999999999999999\r\n", 413),
    ],
    ids=["negative", "non-numeric", "exponent", "missing", "repeated", "chunked", "one-byte-over", "huge"],
)
def test_a_bad_content_length_is_refused_without_reading_a_body(daemon_url: str, headers: str, status: int) -> None:
    # No body follows the headers, so a daemon that tried to read one would stall past the client timeout.
    reply_status, reply = _raw(daemon_url, _post_head(headers), wait_s=2.0)
    assert (reply_status, reply["ok"]) == (status, False)


def test_a_body_that_stops_arriving_is_answered_408(serve: Callable[..., str]) -> None:
    url = serve(request_timeout_s=0.5)
    started = time.monotonic()
    status, reply = _raw(url, _post_head("Content-Length: 100\r\n") + b'{"principal": "adj')
    assert (status, reply["ok"]) == (408, False)
    assert time.monotonic() - started < 3.0


def test_a_body_trickled_in_is_cut_off_at_the_request_deadline(serve: Callable[..., str]) -> None:
    # Each byte lands well inside the socket timeout, so only the whole-request deadline can end this.
    url = serve(request_timeout_s=1.0)
    host, port = url.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=10) as conn:
        conn.sendall(_post_head("Content-Length: 1000\r\n"))
        started = time.monotonic()

        def trickle() -> None:
            for _ in range(40):
                try:
                    conn.sendall(b" ")
                except OSError:
                    return
                time.sleep(0.2)

        threading.Thread(target=trickle, daemon=True).start()
        received = b""
        try:
            while chunk := conn.recv(65536):
                received += chunk
        except ConnectionResetError:
            pass
        ended = time.monotonic() - started
    assert ended < 3.0, ended
    assert received.startswith(b"HTTP/1.1 408 "), received[:80]


def test_connections_past_the_cap_are_refused_before_anything_is_read(serve: Callable[..., str]) -> None:
    url = serve(max_connections=2, request_timeout_s=5.0)
    host, port = url.removeprefix("http://").split(":")
    held = [socket.create_connection((host, int(port))) for _ in range(2)]
    try:
        time.sleep(0.3)  # both accepted, each holding a handler thread while sending nothing
        status, reply = _raw(url, b"", wait_s=2.0)
        assert (status, reply["error"]) == (503, "too many connections")
    finally:
        for conn in held:
            conn.close()
    deadline = time.monotonic() + 3.0
    while True:
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=2) as resp:
                assert resp.status == 200
                break
        except (urllib.error.URLError, ConnectionError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


def test_a_jobs_slot_stays_taken_after_its_reply_until_its_container_is_gone(serve: Callable[..., str]) -> None:
    unconfirmed: list[Callable[[], None]] = []

    def launcher(body: dict[str, Any], *, on_gone: Callable[[], None]) -> dict[str, Any]:
        unconfirmed.append(on_gone)
        return {"ok": True, "result": {}, "killed": None, "ms": 1, "exit": 0}

    url = serve(launcher=launcher)
    assert [_post(url, JOB)[0] for _ in range(3)] == [200, 200, 429]
    unconfirmed.pop()()
    assert _post(url, JOB)[0] == 200


def _engine(up: threading.Event) -> Callable[..., subprocess.CompletedProcess[str]]:
    """A docker CLI whose engine answers only while up is set, and then has no such container."""

    def docker(*args: str, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
        if not up.is_set():
            return subprocess.CompletedProcess(["docker", *args], 1, "", "Cannot connect to the Docker daemon")
        return subprocess.CompletedProcess(["docker", *args], 1, "", f"No such container: {args[-1]}")

    return docker


def test_a_container_docker_cannot_confirm_stopped_stays_tracked_and_keeps_its_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    up = threading.Event()
    # The CLI hangs, as it does when the engine stops answering mid-run.
    monkeypatch.setattr(sandboxd, "docker_argv", lambda *_: [sys.executable, "-c", "import time; time.sleep(60)"])
    monkeypatch.setattr(sandboxd, "_docker", _engine(up))
    monkeypatch.setattr(sandboxd, "STOP_WITHIN_S", 0.2)
    monkeypatch.setattr(sandboxd, "CLI_GRACE_S", 0.2)
    reaper = sandboxd.Reaper(every_s=3600)  # swept by hand below
    monkeypatch.setattr(sandboxd, "REAPER", reaper)
    slots = sandboxd.Slots(per_principal=1, total=1)
    assert slots.acquire("adjuster_mn") is None

    reply = sandboxd.launch(TEMPLATE_JOB, wall_clock_s=0.3, on_gone=functools.partial(slots.release, "adjuster_mn"))

    assert (reply["ok"], reply["killed"], reply["exit"]) == (False, "timeout", None)
    [name] = reaper.pending()
    assert name in sandboxd._live
    assert slots.acquire("adjuster_tx") == 503
    reaper.sweep()
    assert reaper.pending() == {name} and slots.acquire("adjuster_tx") == 503
    up.set()
    reaper.sweep()
    assert reaper.pending() == set() and name not in sandboxd._live
    assert slots.acquire("adjuster_tx") is None


def test_stderr_counts_against_the_same_output_cap_as_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stands in for a job that prints forever: stdout stays empty and stderr alone passes the cap.
    flood = "import sys\nfor _ in range(64):\n    sys.stderr.write('x' * 65536)\n"
    monkeypatch.setattr(sandboxd, "docker_argv", lambda *_: [sys.executable, "-c", flood])
    up = threading.Event()
    up.set()
    monkeypatch.setattr(sandboxd, "_docker", _engine(up))
    reply = sandboxd.launch(TEMPLATE_JOB, output_cap=1 << 20)
    assert (reply["ok"], reply["killed"]) == (False, "output_cap")


TEMPLATE_JOB: dict[str, Any] = {
    "template": "slope",
    "params": {"value": "value", "period": "period"},
    "table": {"columns": ["period", "value"], "rows": [["2024", 1.0], ["2025", 2.0]]},
}
