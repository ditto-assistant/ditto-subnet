"""Wire shape of the #3271 cost ceiling: 4110 and the exchange evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from ditto.api_models.inference import InferenceExchangeResponse
from ditto.api_server.endpoints.inference import InferenceDeclinedError
from ditto.api_server.middleware.error_envelope import (
    ERROR_CODE_INFERENCE_COST_CEILING_EXHAUSTED,
    ERROR_CODE_INFERENCE_TOKEN_BUDGET_EXHAUSTED,
)
from ditto.db.queries.inference import InferenceDecline


@pytest.mark.asyncio
async def test_4110_is_a_terminal_429_carrying_ledger_evidence(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    assert ERROR_CODE_INFERENCE_COST_CEILING_EXHAUSTED == 4110
    assert ERROR_CODE_INFERENCE_TOKEN_BUDGET_EXHAUSTED == 4104

    @app.get("/_test/cost_ceiling")
    async def _raise() -> dict[str, Any]:
        raise InferenceDeclinedError(
            InferenceDecline.COST_CEILING_EXHAUSTED,
            lane="embedding",
            headers={"X-Ditto-Ledger-Cost-Microusd": "1234"},
        )

    response = await client.get("/_test/cost_ceiling")
    assert response.status_code == 429
    body = response.json()
    assert body["error_code"] == 4110
    assert body["message"] == "embedding grant has spent its cost ceiling"
    assert response.headers["X-Ditto-Ledger-Cost-Microusd"] == "1234"
    assert "Retry-After" not in response.headers


@pytest.mark.asyncio
async def test_declines_without_a_live_ceiling_carry_no_ledger_header(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    @app.get("/_test/token_wall")
    async def _raise() -> dict[str, Any]:
        raise InferenceDeclinedError(
            InferenceDecline.TOKEN_BUDGET_EXHAUSTED, lane="inference"
        )

    response = await client.get("/_test/token_wall")
    assert response.json()["error_code"] == 4104
    assert "X-Ditto-Ledger-Cost-Microusd" not in response.headers


def _exchange(**extra: object) -> dict[str, object]:
    return InferenceExchangeResponse(
        grant_id=uuid4(),
        bearer="b" * 43,
        proxy_url="https://platform.example/api/v1/inference/chat/completions",
        expires_at=datetime.now(UTC),
        generation=1,
        request_budget=1,
        token_budget=1,
        **extra,  # type: ignore[arg-type]
    ).model_dump(mode="json")


def test_exchange_omits_the_ceiling_when_none_is_stamped() -> None:
    body = _exchange()
    assert "cost_ceiling_microusd" not in body
    assert "cost_ceiling_mode" not in body


def test_exchange_carries_a_stamped_ceiling() -> None:
    body = _exchange(cost_ceiling_microusd=5_000_000, cost_ceiling_mode="enforce")
    assert body["cost_ceiling_microusd"] == 5_000_000
    assert body["cost_ceiling_mode"] == "enforce"
