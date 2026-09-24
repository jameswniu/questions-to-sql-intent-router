from __future__ import annotations

import asyncio
import datetime
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

DEFAULT_URL = "http://sandboxd:8700"
# The daemon's wall clock is 10 s including container start; a killed job answers in about 10.5 s.
CLIENT_TIMEOUT_S = 12.0


@dataclass(frozen=True)
class Table:
    columns: list[str]
    rows: list[list[Any]] = field(default_factory=list)


@dataclass(frozen=True)
class JobResult:
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None
    killed: str | None = None
    ms: int = 0
    exit: int | None = None
    status: int = 200


class SandboxUnavailable(RuntimeError):
    pass


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    raise TypeError(f"cannot send {type(value).__name__} to the sandbox")


class Sandbox:
    def __init__(self, url: str | None = None, timeout_s: float = CLIENT_TIMEOUT_S) -> None:
        self.url = (url or os.environ.get("SANDBOXD_URL") or DEFAULT_URL).rstrip("/")
        self.timeout_s = timeout_s

    async def run(
        self,
        principal_role: str,
        *,
        template: str | None,
        code: str | None,
        params: dict[str, Any],
        table: Table,
    ) -> JobResult:
        body = {
            "principal": principal_role,
            "template": template,
            "code": code,
            "params": params,
            "table": {"columns": list(table.columns), "rows": [list(r) for r in table.rows]},
        }
        data = json.dumps(body, default=_encode).encode()
        return await asyncio.to_thread(self._post, data)

    def _post(self, data: bytes) -> JobResult:
        request = urllib.request.Request(
            f"{self.url}/run", data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return _result(response.status, response.read())
        except urllib.error.HTTPError as exc:
            # 400, 422, 429 and 503 all carry a JSON body the caller can show.
            return _result(exc.code, exc.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise SandboxUnavailable(f"sandboxd at {self.url} did not answer: {exc}") from exc


def _result(status: int, raw: bytes) -> JobResult:
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return JobResult(ok=False, error=f"sandboxd answered {status} with a body that is not JSON", status=status)
    return JobResult(
        ok=bool(body.get("ok")) and status == 200,
        result=body.get("result"),
        error=body.get("error"),
        killed=body.get("killed"),
        ms=int(body.get("ms") or 0),
        exit=body.get("exit"),
        status=status,
    )
