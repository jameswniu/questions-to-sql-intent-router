# Each hostile job below passes codecheck, reaches the OS through pandas' own module references,
# and runs under the real launch flags, so whatever stops it is the container.
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

from sandbox import sandboxd

pytestmark = pytest.mark.sandbox

OS = "pd.io.common.os"
SUBPROCESS = 'pd.compat.sys.modules["subprocess"]'


def test_code_cannot_open_a_network_socket(run_hostile: Any) -> None:
    reply = run_hostile("""
def run(df, params):
    out = {}
    try:
        pd.read_csv("http://1.1.1.1/claims.csv")
        out["http"] = "fetched"
    except Exception as exc:
        out["http"] = str(exc)
    socket = pd.compat.sys.modules["socket"]
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2).close()
        out["tcp"] = "connected"
    except Exception as exc:
        out["tcp"] = str(exc)
    out["routes"] = pd.read_csv("/proc/net/route", sep="\\t").shape[0]
    return out
""")
    assert reply["ok"], reply
    result = reply["result"]
    assert "Network is unreachable" in result["http"]
    assert "Network is unreachable" in result["tcp"]
    assert result["routes"] == 0  # --network none leaves loopback only, with no route anywhere


def test_process_spawning_stops_at_the_pids_limit(run_hostile: Any) -> None:
    reply = run_hostile(f"""
def run(df, params):
    os = {OS}
    r, w = os.pipe()
    forked, error = 0, None
    while forked < 500:
        try:
            pid = os.fork()
        except Exception as exc:
            error = str(exc)
            break
        if pid == 0:
            os.read(r, 1)  # each child holds its pid until the container ends
        forked += 1
    return {{"forked": forked, "error": error}}
""")
    assert reply["ok"], reply
    assert reply["result"]["forked"] < 64
    assert "Resource temporarily unavailable" in reply["result"]["error"]


def test_a_fork_bomb_is_contained_and_the_next_job_runs_normally(run_hostile: Any) -> None:
    reply = run_hostile(f"""
def run(df, params):
    os = {OS}
    while True:
        try:
            os.fork()
        except Exception:
            pass
""")
    assert reply["killed"] == "timeout" and reply["ms"] < 12_500
    after = sandboxd.launch({"template": "slope", "params": {"value": "value", "period": "period"}, "table": TINY})
    assert after["ok"] and after["ms"] < 5_000


def test_a_memory_hog_is_killed_at_the_memory_limit(run_hostile: Any) -> None:
    reply = run_hostile("""
def run(df, params):
    blob = "x" * (1 << 30)
    return {"n": len(blob)}
""")
    assert reply["killed"] == "oom"
    assert reply["exit"] == 137 and reply["ok"] is False


def test_a_busy_loop_is_killed_by_the_wall_clock(run_hostile: Any) -> None:
    started = time.monotonic()
    reply = run_hostile("""
def run(df, params):
    while True:
        pass
""")
    assert reply["killed"] == "timeout" and reply["ok"] is False
    assert reply["ms"] >= 10_000 and time.monotonic() - started < 12.0


def test_the_root_filesystem_is_read_only_and_tmp_is_writable_but_noexec(run_hostile: Any) -> None:
    reply = run_hostile(f"""
def run(df, params):
    os = {OS}
    out = {{}}
    for path in ("/etc/claims.csv", "/claims.csv", "/runner/templates.py"):
        try:
            df.to_csv(path)
            out[path] = "written"
        except Exception as exc:
            out[path] = str(exc)
    df.to_csv("/tmp/ok.csv", index=False)
    out["tmp_rows"] = len(pd.read_csv("/tmp/ok.csv"))
    fd = os.open("/tmp/hello.sh", os.O_WRONLY | os.O_CREAT, 0o755)
    os.write(fd, b"#!/bin/sh\\necho escaped\\n")
    os.close(fd)
    try:
        out["exec_tmp"] = {SUBPROCESS}.run(["/tmp/hello.sh"], capture_output=True, text=True).stdout
    except Exception as exc:
        out["exec_tmp"] = str(exc)
    return out
""")
    assert reply["ok"], reply
    result = reply["result"]
    for path in ("/etc/claims.csv", "/claims.csv", "/runner/templates.py"):
        assert "Read-only file system" in result[path], (path, result[path])
    assert result["tmp_rows"] == 2
    assert "Permission denied" in result["exec_tmp"]


def test_the_environment_holds_nothing_from_the_host(run_hostile: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPASSWORD", "canary-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:canary-secret@db/claims")
    reply = run_hostile(f"""
def run(df, params):
    return {{"env": dict({OS}.environ)}}
""")
    assert reply["ok"], reply
    env = reply["result"]["env"]
    # The one variable the daemon passes, the image's own ENV, and what docker and Python set.
    assert set(env) == {
        "PYTHONDONTWRITEBYTECODE",
        "OPENBLAS_NUM_THREADS",
        "PATH",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
        "GPG_KEY",
        "HOSTNAME",
        "HOME",
        "LC_CTYPE",
    }
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert not any("canary" in v for v in env.values())


def test_the_job_runs_as_nobody_with_no_capabilities_and_no_docker_socket(run_hostile: Any) -> None:
    reply = run_hostile(f"""
def run(df, params):
    os = {OS}
    fd = os.open("/proc/self/status", os.O_RDONLY)
    status = os.read(fd, 65536).decode()
    os.close(fd)
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    return {{
        "id": {SUBPROCESS}.run(["id"], capture_output=True, text=True).stdout.strip(),
        "cap_eff": fields["CapEff"].strip(),
        "cap_bnd": fields["CapBnd"].strip(),
        "no_new_privs": fields["NoNewPrivs"].strip(),
        "docker_sock": os.path.exists("/var/run/docker.sock"),
    }}
""")
    assert reply["ok"], reply
    result = reply["result"]
    assert result["id"].startswith("uid=65534(nobody) gid=65534(nogroup)")
    assert result["cap_eff"] == result["cap_bnd"] == "0000000000000000"
    assert result["no_new_privs"] == "1"
    assert result["docker_sock"] is False


def test_oversized_output_is_cut_off_at_the_cap(run_hostile: Any) -> None:
    reply = run_hostile("""
def run(df, params):
    return {"blob": "x" * (2 << 20)}
""")
    assert reply["killed"] == "output_cap" and reply["ok"] is False


def test_a_print_flood_is_cut_off_at_the_output_cap_and_never_reaches_a_host_log(run_hostile: Any) -> None:
    started = time.monotonic()
    reply = run_hostile("""
def run(df, params):
    while True:
        print("x" * 65536)
""")
    assert reply["killed"] == "output_cap" and reply["ok"] is False
    assert time.monotonic() - started < 5.0
    argv = sandboxd.docker_argv("claims-qa-sbx-test")
    assert argv[argv.index("--log-driver") + 1] == "none"


def test_the_container_ends_a_busy_loop_itself_when_the_daemon_never_kills_it(
    run_hostile: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandboxd, "_kill", lambda name: None)
    monkeypatch.setattr(sandboxd, "REAPER", sandboxd.Reaper(every_s=0.25))
    gone = threading.Event()
    started = time.monotonic()
    reply = run_hostile(
        """
def run(df, params):
    while True:
        pass
""",
        wall_clock_s=2.0,
        on_gone=gone.set,
    )
    assert reply["killed"] == "timeout" and reply["ok"] is False
    assert gone.wait(timeout=15), "docker never reported the container stopped"
    # Nothing sent a kill, so what ended it is the deadline the container runs its job under.
    assert time.monotonic() - started >= 2.0 + sandboxd.DEADLINE_GRACE_S


def test_a_third_concurrent_job_from_one_principal_gets_429(sandbox_image: str, daemon_url: str) -> None:
    slow = """
def run(df, params):
    start = pd.Timestamp.now()
    while (pd.Timestamp.now() - start).total_seconds() < 4:
        pass
    return {"done": True}
"""
    first_two: list[tuple[int, dict[str, Any]]] = []

    def submit() -> None:
        first_two.append(_post(daemon_url, {**CODE_JOB, "code": slow}))

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(1.0)
    third = _post(daemon_url, {**CODE_JOB, "code": slow})
    other_principal = _post(daemon_url, {**TEMPLATE_JOB, "principal": "adjuster_tx"})
    for t in threads:
        t.join()

    assert third[0] == 429, third
    assert other_principal[0] == 200 and other_principal[1]["ok"], other_principal
    assert [status for status, _ in first_two] == [200, 200]
    assert all(body["ok"] for _, body in first_two)


def test_the_runner_restriction_turns_a_forbidden_builtin_into_a_legible_error(sandbox_image: str) -> None:
    # Launched directly, skipping codecheck, to show the second layer on its own.
    reply = sandboxd.launch(
        {"code": "def run(df, params):\n    return {'x': open('/etc/passwd').read()}", "table": TINY}
    )
    assert reply["ok"] is False and reply["killed"] is None and reply["exit"] == 0
    assert reply["error"] == "NameError: name 'open' is not defined"


def test_a_template_job_round_trips_through_the_container(sandbox_image: str) -> None:
    table = {
        "columns": ["yr", "paid"],
        "rows": [[2024, 100.0], [2024, 200.0], [2025, 150.0], [2025, 150.0], [2025, 300.0]],
    }
    reply = sandboxd.launch(
        {
            "template": "decompose",
            "params": {"value": "paid", "period": "yr", "base": 2024, "current": 2025},
            "table": table,
        }
    )
    assert reply["ok"], reply
    assert (reply["result"]["count_effect"], reply["result"]["mean_effect"]) == (175.0, 125.0)
    assert reply["killed"] is None and reply["exit"] == 0


TINY = {"columns": ["period", "value"], "rows": [["2024", 1.0], ["2025", 2.0]]}
TEMPLATE_JOB = {
    "principal": "adjuster_mn",
    "template": "slope",
    "params": {"value": "value", "period": "period"},
    "table": TINY,
}
CODE_JOB = {"principal": "adjuster_mn", "template": None, "params": {}, "table": TINY}


def _post(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url + "/run", data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())
