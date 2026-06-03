"""Unit tests for :mod:`ditto.api_server.endpoints.upload`."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import bittensor
import httpx
import pytest
from fastapi import FastAPI

from ditto.api_server.endpoints.upload import (
    ERROR_CODE_BAD_SIGNATURE,
    ERROR_CODE_HOTKEY_NOT_REGISTERED,
    ERROR_CODE_TARBALL_TOO_LARGE,
)
from ditto.api_server.middleware.error_envelope import (
    ERROR_CODE_MALFORMED_PRICE,
    ERROR_CODE_ORACLE_UNREACHABLE,
    ERROR_CODE_PAYMENT_AMOUNT_MISMATCH,
    ERROR_CODE_PAYMENT_CALL_TYPE_MISMATCH,
    ERROR_CODE_PAYMENT_DESTINATION_MISMATCH,
    ERROR_CODE_PAYMENT_EXTRINSIC_FAILED,
    ERROR_CODE_PAYMENT_NOT_FOUND,
    ERROR_CODE_PAYMENT_REPLAYED,
    ERROR_CODE_PAYMENT_SIGNER_MISMATCH,
    ERROR_CODE_PRICE_TOO_STALE,
)
from ditto.api_server.payment_verifier import (
    PaymentAmountMismatch,
    PaymentCallTypeMismatch,
    PaymentDestinationMismatch,
    PaymentExtrinsicFailed,
    PaymentNotFoundOnChain,
    PaymentReplayedError,
    PaymentSignerMismatch,
    VerifiedPayment,
)
from ditto.api_server.pricing import (
    OracleUnreachableError,
    PriceTooStaleError,
)
from ditto.api_server.storage import ObjectUploadFailedError
from ditto.chain.errors import ChainConnectionError
from ditto.tests.api_server.conftest import (
    override_get_chain_client,
    override_get_price_oracle,
    override_get_storage_client,
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
        assert "/api/v1/upload/agent" in paths


_GOOD_TAR_BYTES = b"\x1f\x8b" + b"x" * 1024  # gzip magic + padding
_GOOD_TAR_SHA = hashlib.sha256(_GOOD_TAR_BYTES).hexdigest()
_GOOD_BLOCK_HASH = "0x" + "ab" * 32


def _make_verified_payment(**overrides: Any) -> VerifiedPayment:
    base: dict[str, Any] = {
        "block_hash": _GOOD_BLOCK_HASH,
        "extrinsic_index": 7,
        "miner_hotkey": "5Hotkey",  # overridden in fixtures
        "miner_coldkey": "5Coldkey",
        "amount_rao": 17_500_000,
        "dest_address": "5SendAddress",
        "block_timestamp": datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return VerifiedPayment(**base)


def _override_payment_verifier(
    app: FastAPI,
    *,
    verified: VerifiedPayment | None = None,
    raises: Exception | None = None,
) -> MagicMock:
    """Install a verifier override returning canned VerifiedPayment or raising."""
    from ditto.api_server.dependencies import get_payment_verifier

    verifier = MagicMock()
    if raises is not None:
        verifier.verify_payment = AsyncMock(side_effect=raises)
    else:
        verifier.verify_payment = AsyncMock(
            return_value=verified if verified is not None else _make_verified_payment()
        )

    async def _fake_verifier() -> MagicMock:
        return verifier

    app.dependency_overrides[get_payment_verifier] = _fake_verifier
    return verifier


def _override_session_writes(app: FastAPI) -> MagicMock:
    """Install a session whose write methods are no-ops.

    Lets routes that go through ``async with session.begin():`` succeed
    without a real DB connection. Returns the mock so tests can stub
    specific behaviours (e.g. raise on session.add) afterward.
    """
    from ditto.api_server.dependencies import get_session

    session = MagicMock()
    session.add = MagicMock(return_value=None)
    session.flush = AsyncMock(return_value=None)
    session.execute = AsyncMock(return_value=None)
    begin = MagicMock()
    begin.__aenter__ = AsyncMock(return_value=session)
    begin.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin)

    async def _fake_session():
        yield session

    app.dependency_overrides[get_session] = _fake_session
    return session


def _override_session_raise_on_insert(app: FastAPI, raises: Exception) -> None:
    """Install a write-capable session that raises ``raises`` from ``session.add``.

    Models the case where queries-layer raise paths fire inside the
    ``async with session.begin()`` block.
    """
    session = _override_session_writes(app)
    session.add = MagicMock(side_effect=raises)


def _upload_agent_form(
    *,
    keypair: bittensor.Keypair | None = None,
    sha256: str = _GOOD_TAR_SHA,
    name: str = "alpha-agent",
    override_hotkey: str | None = None,
    payment_block_hash: str = _GOOD_BLOCK_HASH,
    payment_block_number: int = 13579,
    payment_extrinsic_index: int = 7,
) -> tuple[dict[str, Any], dict[str, tuple[str, bytes, str]]]:
    kp = keypair or bittensor.Keypair.create_from_uri("//Alice")
    hotkey = override_hotkey or kp.ss58_address
    payload = f"{hotkey}:{sha256}".encode()
    signature_hex = kp.sign(payload).hex()
    data: dict[str, Any] = {
        "hotkey": hotkey,
        "sha256": sha256,
        "name": name,
        "signature": signature_hex,
        "payment_block_hash": payment_block_hash,
        "payment_block_number": payment_block_number,
        "payment_extrinsic_index": payment_extrinsic_index,
    }
    files = {"agent_tar": ("harness.tar.gz", _GOOD_TAR_BYTES, "application/gzip")}
    return data, files


def _wire_full_stack(app: FastAPI) -> dict[str, MagicMock]:
    """One-call shorthand that overrides every dep the endpoint touches."""
    storage = override_get_storage_client(app)
    verifier = _override_payment_verifier(app)
    session = _override_session_writes(app)
    override_get_chain_client(app)
    return {"storage": storage, "verifier": verifier, "session": session}


class TestUploadAgentHappyPath:
    async def test_returns_agent_id_and_uploaded_status(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        _override_payment_verifier(
            app, verified=_make_verified_payment(miner_hotkey=kp.ss58_address)
        )
        data, files = _upload_agent_form(keypair=kp)

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 200, response.text
        body = response.json()
        assert "agent_id" in body
        assert body["status"] == "uploaded"

    async def test_stores_tar_under_agent_id_key(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        deps = _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        _override_payment_verifier(
            app, verified=_make_verified_payment(miner_hotkey=kp.ss58_address)
        )
        data, files = _upload_agent_form(keypair=kp)

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 200, response.text
        put_kwargs = deps["storage"].put_object.await_args.kwargs
        agent_id = response.json()["agent_id"]
        assert put_kwargs["key"] == f"{agent_id}/agent.tar.gz"
        assert put_kwargs["content_type"] == "application/gzip"
        assert put_kwargs["body"] == _GOOD_TAR_BYTES


class TestUploadAgentValidationFailures:
    async def test_bad_signature_returns_400(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        _wire_full_stack(app)
        data, files = _upload_agent_form()
        data["signature"] = "a" * 128  # valid hex shape, wrong sig

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 400
        assert "signature" in response.json()["message"]

    async def test_hotkey_not_registered_returns_400(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        _wire_full_stack(app)

        async def _fake_chain() -> MagicMock:
            chain = MagicMock()
            chain.is_registered = AsyncMock(return_value=False)
            return chain

        from ditto.api_server.dependencies import get_chain_client

        app.dependency_overrides[get_chain_client] = _fake_chain
        data, files = _upload_agent_form()

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 400
        assert "not registered" in response.json()["message"]

    async def test_chain_unreachable_returns_503(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        from ditto.api_server.middleware.error_envelope import (
            ERROR_CODE_HTTP_EXCEPTION,
        )

        _wire_full_stack(app)
        override_get_chain_client(app, raises=ChainConnectionError("pylon down"))
        data, files = _upload_agent_form()

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 503
        # Pinned: the chain-unreachable path uses HTTPException(503) which
        # surfaces via the generic _http_exception_handler. A future move
        # to a chain-specific envelope handler would need this assertion
        # updated alongside the new code.
        assert response.json()["error_code"] == ERROR_CODE_HTTP_EXCEPTION

    async def test_sha_mismatch_returns_400(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        _wire_full_stack(app)
        # Claim a different sha than the actual bytes will hash to.
        bogus_sha = "ff" * 32
        kp = bittensor.Keypair.create_from_uri("//Alice")
        payload = f"{kp.ss58_address}:{bogus_sha}".encode()
        data = {
            "hotkey": kp.ss58_address,
            "sha256": bogus_sha,
            "name": "alpha-agent",
            "signature": kp.sign(payload).hex(),
            "payment_block_hash": _GOOD_BLOCK_HASH,
            "payment_block_number": 13579,
            "payment_extrinsic_index": 7,
        }
        files = {"agent_tar": ("harness.tar.gz", _GOOD_TAR_BYTES, "application/gzip")}

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 400
        assert "sha256" in response.json()["message"]

    async def test_oversized_tarball_returns_413(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        _wire_full_stack(app)
        oversized = b"\x1f\x8b" + b"x" * (3 * 1024 * 1024)
        big_sha = hashlib.sha256(oversized).hexdigest()
        kp = bittensor.Keypair.create_from_uri("//Alice")
        payload = f"{kp.ss58_address}:{big_sha}".encode()
        data = {
            "hotkey": kp.ss58_address,
            "sha256": big_sha,
            "name": "alpha-agent",
            "signature": kp.sign(payload).hex(),
            "payment_block_hash": _GOOD_BLOCK_HASH,
            "payment_block_number": 13579,
            "payment_extrinsic_index": 7,
        }
        files = {"agent_tar": ("harness.tar.gz", oversized, "application/gzip")}

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 413

    @pytest.mark.parametrize(
        ("missing_field",),
        [
            ("hotkey",),
            ("sha256",),
            ("name",),
            ("signature",),
            ("payment_block_hash",),
            ("payment_block_number",),
            ("payment_extrinsic_index",),
        ],
    )
    async def test_missing_field_returns_422(
        self, app: FastAPI, client: httpx.AsyncClient, missing_field: str
    ):
        _wire_full_stack(app)
        data, files = _upload_agent_form()
        data.pop(missing_field)

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 422

    async def test_malformed_hotkey_returns_422(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        _wire_full_stack(app)
        data, files = _upload_agent_form()
        data["hotkey"] = "not-ss58"

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 422


class TestUploadAgentPaymentVerifierBranches:
    """Each PaymentVerifierError subclass propagates to the typed envelope."""

    @pytest.mark.parametrize(
        ("exc", "expected_code"),
        [
            (PaymentNotFoundOnChain("nope"), ERROR_CODE_PAYMENT_NOT_FOUND),
            (PaymentExtrinsicFailed("failed"), ERROR_CODE_PAYMENT_EXTRINSIC_FAILED),
            (PaymentAmountMismatch("band"), ERROR_CODE_PAYMENT_AMOUNT_MISMATCH),
            (
                PaymentDestinationMismatch("dest"),
                ERROR_CODE_PAYMENT_DESTINATION_MISMATCH,
            ),
            (PaymentSignerMismatch("signer"), ERROR_CODE_PAYMENT_SIGNER_MISMATCH),
            (PaymentCallTypeMismatch("call"), ERROR_CODE_PAYMENT_CALL_TYPE_MISMATCH),
        ],
    )
    async def test_typed_payment_error_maps_to_envelope(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        exc: Exception,
        expected_code: int,
    ):
        _wire_full_stack(app)
        _override_payment_verifier(app, raises=exc)
        data, files = _upload_agent_form()

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 402
        assert response.json()["error_code"] == expected_code


class TestUploadAgentReplayHandling:
    async def test_payment_replay_returns_402_3207(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        _wire_full_stack(app)
        _override_session_raise_on_insert(
            app, PaymentReplayedError("payment proof already used")
        )
        data, files = _upload_agent_form()

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 402
        assert response.json()["error_code"] == ERROR_CODE_PAYMENT_REPLAYED


class TestUploadAgentStorageFailure:
    async def test_storage_failure_returns_5xx(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        _wire_full_stack(app)
        override_get_storage_client(app, raises=ObjectUploadFailedError("s3 down"))
        data, files = _upload_agent_form()

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        # ObjectUploadFailedError is unhandled by the envelope handlers,
        # so it falls through to the generic 500 path.
        assert response.status_code == 500


class TestUploadAgentDbFailure:
    async def test_agent_insert_db_integrity_error_returns_5xx(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        """Any non-replay constraint violation on the agents insert (e.g.
        UNIQUE(agent_id, miner_hotkey), CHECK, NOT NULL) is programmer-bug
        territory; the envelope catch-all must surface a 500 rather than
        accidentally classifying it as something miner-facing."""
        from ditto.db import IntegrityError as DbIntegrityError

        _wire_full_stack(app)
        _override_session_raise_on_insert(
            app, DbIntegrityError("agent constraint violation")
        )
        data, files = _upload_agent_form()

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 500


class TestUploadAgentChainOutageDuringVerify:
    async def test_chain_error_during_verify_returns_503(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        """Pylon hiccup mid-verify must surface as 503 (same as the
        chain.is_registered path) instead of falling through to the
        unhandled-exception 500. Mirrors the shipped /upload/check
        contract around chain outages."""
        _wire_full_stack(app)
        _override_payment_verifier(
            app, raises=ChainConnectionError("pylon down mid-verify")
        )
        data, files = _upload_agent_form()

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 503
