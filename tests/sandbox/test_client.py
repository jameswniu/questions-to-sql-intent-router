from __future__ import annotations

import datetime
import socket
from decimal import Decimal

import pytest

from app.sandbox.client import Sandbox, SandboxUnavailable, Table


async def test_client_reports_a_codecheck_refusal_as_a_failed_result(daemon_url: str) -> None:
    table = Table(columns=["paid_date", "amount"], rows=[[datetime.date(2025, 3, 1), Decimal("1250.10")]])
    result = await Sandbox(daemon_url).run(
        "adjuster_mn", template=None, code="import os\ndef run(df, params):\n    return {}", params={}, table=table
    )
    assert result.ok is False and result.status == 422
    assert result.error and "import" in result.error


async def test_client_raises_when_the_daemon_is_unreachable() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(SandboxUnavailable):
        await Sandbox(f"http://127.0.0.1:{port}", timeout_s=2).run(
            "adjuster_mn", template="slope", code=None, params={}, table=Table(columns=[])
        )


@pytest.mark.sandbox
async def test_client_runs_a_template_as_the_principal_through_the_container(
    sandbox_image: str, daemon_url: str
) -> None:
    table = Table(
        columns=["month", "paid"],
        rows=[["2025-01", Decimal("100.00")], ["2025-02", Decimal("130.00")], ["2025-03", Decimal("160.00")]],
    )
    result = await Sandbox(daemon_url).run(
        "adjuster_mn", template="slope", code=None, params={"value": "paid", "period": "month"}, table=table
    )
    assert result.ok, result
    assert result.result and result.result["slope"] == pytest.approx(30.0)
    assert result.killed is None and result.exit == 0 and result.ms > 0
