from __future__ import annotations

import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from typing import Any, NoReturn

import pytest

from app.sandbox.codecheck import check
from sandbox import sandboxd

TINY_TABLE = {"columns": ["period", "value"], "rows": [["2024", 1.0], ["2025", 2.0]]}
# make test-sandbox sets this, so a host without Docker or the image fails instead of skipping every limit test.
REQUIRE_DOCKER = os.environ.get("REQUIRE_DOCKER") == "1"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "sandbox: needs Docker and the claims-qa-sandbox:1 image")


def _unavailable(reason: str) -> NoReturn:
    if REQUIRE_DOCKER:
        pytest.fail(f"REQUIRE_DOCKER=1 but {reason}", pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def sandbox_image() -> str:
    if shutil.which("docker") is None:
        _unavailable("the docker CLI is not on PATH")
    probe = subprocess.run(["docker", "image", "inspect", sandboxd.IMAGE], capture_output=True, timeout=20, check=False)
    if probe.returncode != 0:
        _unavailable(f"{sandboxd.IMAGE} is not built (docker build -f docker/sandbox.Dockerfile -t {sandboxd.IMAGE} .)")
    return sandboxd.IMAGE


@pytest.fixture
def run_hostile(sandbox_image: str) -> Any:
    """Launch code through the real container flags, after proving the allow-list lets it through."""

    def run(code: str, table: dict[str, Any] | None = None, **launch_options: Any) -> dict[str, Any]:
        verdict = check(code)
        assert verdict.ok, f"the test code must pass codecheck so that only the container stops it: {verdict.reason}"
        job = {"template": None, "code": code, "params": {}, "table": table or TINY_TABLE}
        return sandboxd.launch(job, **launch_options)

    return run


@pytest.fixture
def serve() -> Iterator[Callable[..., str]]:
    """Starts a daemon on a free port with the given SandboxServer options and returns its URL."""
    servers: list[sandboxd.SandboxServer] = []

    def start(**options: Any) -> str:
        server = sandboxd.SandboxServer(("127.0.0.1", 0), **options)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def daemon_url(serve: Callable[..., str]) -> str:
    return serve()
