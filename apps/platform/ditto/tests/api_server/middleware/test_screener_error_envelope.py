"""Screener conflict reasons are logged without changing the public envelope."""

import logging

import httpx
import pytest
from fastapi import FastAPI

from ditto.api_server.endpoints.screener import AgentNotScreenableError
from ditto.api_server.middleware.error_envelope import register_exception_handlers


async def test_not_screenable_logs_private_reason_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = FastAPI()
    register_exception_handlers(app)
    path = "/api/v1/screener/agent/test/result"

    @app.post(path)
    async def result() -> None:
        raise AgentNotScreenableError(
            "screening attempt is expired or already completed"
        )

    with caplog.at_level(logging.WARNING):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(path)

    assert response.status_code == 409
    assert response.json() == {
        "error_code": 5001,
        "message": "agent is not screenable",
        "request_id": "-",
    }
    records = [record for record in caplog.records if "not in screenable" in record.msg]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert path in records[0].message
    assert "expired or already completed" in records[0].message
