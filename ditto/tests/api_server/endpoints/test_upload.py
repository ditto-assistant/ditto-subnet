"""Unit tests for :mod:`ditto.api_server.endpoints.upload`."""

from __future__ import annotations

from decimal import Decimal

import bittensor
import httpx
from fastapi import FastAPI

from ditto.api_server.endpoints.upload import (
    ERROR_CODE_BAD_SIGNATURE,
    ERROR_CODE_HOTKEY_NOT_REGISTERED,
    ERROR_CODE_TARBALL_TOO_LARGE,
)
from ditto.api_server.middleware.error_envelope import (
    ERROR_CODE_MALFORMED_PRICE,
    ERROR_CODE_ORACLE_UNREACHABLE,
    ERROR_CODE_PRICE_TOO_STALE,
)
from ditto.api_server.pricing import (
    OracleUnreachableError,
    PriceTooStaleError,
)
from ditto.chain.errors import ChainConnectionError
from ditto.tests.api_server.conftest import (
    override_get_chain_client,
    override_get_price_oracle,
)

_GOOD_SHA256 = "1d8a3b6f04e2c7f9a51bd3e5c8f2a7b06d4e9c1f2a3b4c5d6e7f8a9b0c1d2e3f"
_BAD_SIG = "a" * 128  # 64 bytes of 0xaa; valid hex but won't verify


def _make_keypair() -> bittensor.Keypair:
    """Deterministic test keypair via the well-known //Alice URI."""
    return bittensor.Keypair.create_from_uri("//Alice")


def _signed_request_body(
    *,
    keypair: bittensor.Keypair | None = None,
    sha256: str = _GOOD_SHA256,
    file_size_bytes: int = 1_000_000,
    override_hotkey: str | None = None,
) -> dict[str, object]:
    kp = keypair or _make_keypair()
    hotkey = override_hotkey or kp.ss58_address
    payload = f"{hotkey}:{sha256}".encode()
    return {
        "hotkey": hotkey,
        "sha256": sha256,
        "file_size_bytes": file_size_bytes,
        "signature": kp.sign(payload).hex(),
    }


class TestEvalPricing:
    async def test_happy_path(self, app: FastAPI, client: httpx.AsyncClient):
        override_get_price_oracle(app, price_usd=Decimal("400"))
        response = await client.get("/api/v1/upload/eval-pricing")
        assert response.status_code == 200
        body = response.json()
        # $5 fee × 1.4 buffer / $400 price = 0.0175 TAO = 17_500_000 rao
        assert body["amount_rao"] == 17_500_000
        assert body["send_address"].startswith("5")

    async def test_oracle_down_returns_503_with_specific_error_code(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        override_get_price_oracle(app, raises=OracleUnreachableError("down"))
        response = await client.get("/api/v1/upload/eval-pricing")
        assert response.status_code == 503
        assert response.json()["error_code"] == ERROR_CODE_ORACLE_UNREACHABLE

    async def test_oracle_stale_returns_503_with_specific_error_code(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        override_get_price_oracle(app, raises=PriceTooStaleError("stale"))
        response = await client.get("/api/v1/upload/eval-pricing")
        assert response.status_code == 503
        assert response.json()["error_code"] == ERROR_CODE_PRICE_TOO_STALE

    async def test_zero_amount_rao_returns_malformed_price_envelope(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        # An absurdly high TAO/USD price would make the rao math truncate to 0.
        from decimal import Decimal

        override_get_price_oracle(app, price_usd=Decimal("1e30"))
        response = await client.get("/api/v1/upload/eval-pricing")
        assert response.status_code == 503
        assert response.json()["error_code"] == ERROR_CODE_MALFORMED_PRICE

    async def test_response_uses_configured_payment_address(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        override_get_price_oracle(app, price_usd=Decimal("400"))
        response = await client.get("/api/v1/upload/eval-pricing")
        # The conftest fixture sets a known address.
        assert response.json()["send_address"] == (
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
        )


class TestUploadCheck:
    async def test_happy_path(self, app: FastAPI, client: httpx.AsyncClient):
        # is_registered=True by default in the fake chain client.
        override_get_chain_client(app)
        body = _signed_request_body()
        response = await client.post("/api/v1/upload/check", json=body)
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["error_codes"] == []
        assert result["messages"] == []

    async def test_bad_signature_returns_1100(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        override_get_chain_client(app)
        body = _signed_request_body()
        body["signature"] = _BAD_SIG  # tamper
        response = await client.post("/api/v1/upload/check", json=body)
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is False
        assert ERROR_CODE_BAD_SIGNATURE in result["error_codes"]

    async def test_unregistered_hotkey_returns_1101(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        # Switch the chain mock to report not-registered.
        from unittest.mock import AsyncMock, MagicMock

        from ditto.api_server.dependencies import get_chain_client

        async def _fake_chain() -> MagicMock:
            chain = MagicMock()
            chain.is_registered = AsyncMock(return_value=False)
            return chain

        app.dependency_overrides[get_chain_client] = _fake_chain
        body = _signed_request_body()
        response = await client.post("/api/v1/upload/check", json=body)
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is False
        assert ERROR_CODE_HOTKEY_NOT_REGISTERED in result["error_codes"]

    async def test_tarball_too_large_returns_1102(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        override_get_chain_client(app)
        body = _signed_request_body(file_size_bytes=3 * 1024 * 1024)  # 3 MB
        response = await client.post("/api/v1/upload/check", json=body)
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is False
        assert ERROR_CODE_TARBALL_TOO_LARGE in result["error_codes"]

    async def test_multiple_failures_aggregate(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        from unittest.mock import AsyncMock, MagicMock

        from ditto.api_server.dependencies import get_chain_client

        async def _fake_chain() -> MagicMock:
            chain = MagicMock()
            chain.is_registered = AsyncMock(return_value=False)
            return chain

        app.dependency_overrides[get_chain_client] = _fake_chain
        body = _signed_request_body(file_size_bytes=3 * 1024 * 1024)
        body["signature"] = _BAD_SIG
        response = await client.post("/api/v1/upload/check", json=body)
        result = response.json()
        assert result["ok"] is False
        # All three failure codes present.
        assert ERROR_CODE_BAD_SIGNATURE in result["error_codes"]
        assert ERROR_CODE_HOTKEY_NOT_REGISTERED in result["error_codes"]
        assert ERROR_CODE_TARBALL_TOO_LARGE in result["error_codes"]
        assert len(result["messages"]) == 3

    async def test_chain_error_returns_503(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        override_get_chain_client(app, raises=ChainConnectionError("pylon down"))
        body = _signed_request_body()
        response = await client.post("/api/v1/upload/check", json=body)
        assert response.status_code == 503

    async def test_passes_configured_netuid_to_chain_client(self):
        # Build an app with a non-default netuid and assert it flows
        # through to chain.is_registered + the failure message.
        from dataclasses import replace
        from unittest.mock import AsyncMock, MagicMock

        from ditto.api_server import create_api_server
        from ditto.api_server.dependencies import get_chain_client
        from ditto.tests.api_server.conftest import make_api_server_config

        base = make_api_server_config()
        cfg = replace(base, chain=replace(base.chain, netuid=999))
        custom_app = create_api_server(cfg)
        custom_app.state.commit_hash = "test-commit"

        recorded: dict[str, int] = {}

        async def _fake_chain() -> MagicMock:
            chain = MagicMock()

            async def _is_registered(_hotkey: str, *, netuid: int) -> bool:
                recorded["netuid"] = netuid
                return False

            chain.is_registered = AsyncMock(side_effect=_is_registered)
            return chain

        custom_app.dependency_overrides[get_chain_client] = _fake_chain
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=custom_app, raise_app_exceptions=False),
            base_url="http://test",
        ) as c:
            body = _signed_request_body()
            response = await c.post("/api/v1/upload/check", json=body)

        assert recorded["netuid"] == 999
        assert "netuid 999" in " ".join(response.json()["messages"])


class TestOpenApiInclusion:
    """``/upload/*`` IS in the schema (consumer surface), unlike ops endpoints."""

    async def test_paths_present(self, client: httpx.AsyncClient):
        schema = (await client.get("/openapi.json")).json()
        paths = schema["paths"]
        assert "/api/v1/upload/eval-pricing" in paths
        assert "/api/v1/upload/check" in paths
