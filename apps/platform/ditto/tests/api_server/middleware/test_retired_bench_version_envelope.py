"""How a retired-era score submission is CLASSIFIED on the wire.

The floor itself is asserted against Postgres in
``ditto.tests.db.test_bench_version_floor``. This file covers the other half,
which is just as load-bearing: what the validator is told when it submits a
score for an era that no longer exists.

Getting the classification wrong here is worse than not having the floor. A
validator that reads the refusal as an infrastructure fault hands the ticket
back as ``fail_job(reason="infrastructure")``, and infrastructure is NO-FAULT
on this platform -- it mints a compensating grant, raises the attempt cap and
re-leases. The condition would never clear, so it would re-lease forever. That
is the ``mnemo*`` failure mode (ditto-subnet#279), where twelve dead leases
burned 4.5 validator-hours apiece because a terminal fault was reported as
retryable infrastructure.

So the contract is: **410 Gone, error code 4002, terminal.** Not 409, which
reads as a conflict and invites a retry; not 5xx, which reads as the platform
being broken.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from ditto.api_server.endpoints.validator import RetiredBenchVersionError
from ditto.api_server.middleware.error_envelope import (
    ERROR_CODE_BENCH_VERSION_RETIRED,
    register_exception_handlers,
)


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    register_exception_handlers(application)

    @application.get("/boom")
    async def _boom() -> None:
        raise RetiredBenchVersionError("benchmark v6 is retired")

    return application


async def test_a_retired_era_is_reported_as_gone_not_as_a_conflict(
    app: FastAPI,
) -> None:
    """410 + 4002, and the status is the part that matters.

    ``AgentNotEvaluatableError`` next door maps to 409 because it CLEARS -- the
    agent advances and the score becomes acceptable. A retired era never
    clears, so it must not share a status that means "try again later".
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/boom")

    assert response.status_code == 410
    body = response.json()
    assert body["error_code"] == ERROR_CODE_BENCH_VERSION_RETIRED
    assert body["error_code"] == 4002


async def test_the_refusal_is_not_a_server_error(app: FastAPI) -> None:
    """It must never surface as 5xx.

    A 5xx is exactly what a validator's ``PlatformError`` handling treats as
    the platform being unwell, and on the confirmation lane that routes
    straight to ``infrastructure``. This is a deliberate, correct refusal of a
    well-formed request, so it belongs in the 4xx range.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/boom")

    assert 400 <= response.status_code < 500
