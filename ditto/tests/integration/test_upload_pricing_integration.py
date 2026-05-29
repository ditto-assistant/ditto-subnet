"""Integration tests for ``/upload/eval-pricing`` + ``/upload/check``.

Exercises the real api_server lifespan (real DB engine, real chain
client). The pricing oracle is overridden via ``dependency_overrides``
so the test does not depend on CoinGecko's availability + rate limits.

Run via ``make test-integration`` (excluded from the default suite).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import bittensor
import httpx
import pytest
from fastapi import FastAPI

from ditto.api_server import create_api_server, parse_api_server_config_from_env
from ditto.api_server.dependencies import get_price_oracle

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _running_app() -> AsyncIterator[FastAPI]:
    config = parse_api_server_config_from_env(commit_hash="integration-test")
    app = create_api_server(config)

    # Substitute the pricing oracle so the integration test does not
    # depend on CoinGecko being reachable + rate-limited.
    async def _fake_oracle() -> MagicMock:
        oracle = MagicMock()
        oracle.get_tao_usd = AsyncMock(return_value=Decimal("400"))
        return oracle

    app.dependency_overrides[get_price_oracle] = _fake_oracle

    async with app.router.lifespan_context(app):
        yield app


class TestUploadPricingIntegration:
    async def test_eval_pricing_returns_amount_and_address(self):
        async with (
            _running_app() as app,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            response = await client.get("/api/v1/upload/eval-pricing")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["amount_rao"] > 0
        assert body["send_address"].startswith("5")

    async def test_check_happy_path_against_real_chain(self):
        """Uses a //Alice keypair, which is NOT registered on real netuid 118,
        so we expect ok=false with the not-registered code; signature should
        still verify."""
        keypair = bittensor.Keypair.create_from_uri("//Alice")
        sha256 = "1d8a3b6f04e2c7f9a51bd3e5c8f2a7b06d4e9c1f2a3b4c5d6e7f8a9b0c1d2e3f"
        payload = f"{keypair.ss58_address}:{sha256}".encode()
        body = {
            "hotkey": keypair.ss58_address,
            "sha256": sha256,
            "file_size_bytes": 1000,
            "signature": keypair.sign(payload).hex(),
        }

        async with (
            _running_app() as app,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            response = await client.post("/api/v1/upload/check", json=body)

        assert response.status_code == 200, response.text
        result = response.json()
        # //Alice will not be a registered netuid-118 hotkey on finney.
        assert result["ok"] is False
        assert 1101 in result["error_codes"]
        # But the signature itself must have verified.
        assert 1100 not in result["error_codes"]
