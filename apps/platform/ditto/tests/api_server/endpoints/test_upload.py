"""Unit tests for :mod:`ditto.api_server.endpoints.upload`."""

from __future__ import annotations

import hashlib
import io
import tarfile
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.endpoints.upload import (
    ERROR_CODE_BAD_SIGNATURE,
    ERROR_CODE_HOTKEY_NOT_REGISTERED,
    ERROR_CODE_IDENTICAL_SUBMISSION,
    ERROR_CODE_SUBMISSION_COOLDOWN,
    ERROR_CODE_TARBALL_TOO_LARGE,
    MAX_TARBALL_SIZE_BYTES,
)
from ditto.api_server.middleware.error_envelope import (
    ERROR_CODE_PAYMENT_AMOUNT_MISMATCH,
    ERROR_CODE_PAYMENT_CALL_TYPE_MISMATCH,
    ERROR_CODE_PAYMENT_DESTINATION_MISMATCH,
    ERROR_CODE_PAYMENT_EXTRINSIC_FAILED,
    ERROR_CODE_PAYMENT_NOT_FOUND,
    ERROR_CODE_PAYMENT_RECOVERY_EXPIRED,
    ERROR_CODE_PAYMENT_REPLAYED,
    ERROR_CODE_PAYMENT_SIGNER_MISMATCH,
)
from ditto.api_server.payment_verifier import (
    PaymentAmountMismatch,
    PaymentCallTypeMismatch,
    PaymentDestinationMismatch,
    PaymentExtrinsicFailed,
    PaymentNotFoundOnChain,
    PaymentRecoveryExpired,
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
from ditto.db.queries.agents import SubmissionCooldownError
from ditto.tests.api_server.conftest import (
    override_get_chain_client,
    override_get_embedder,
    override_get_price_oracle,
    override_get_session,
    override_get_storage_client,
)


@pytest.fixture(autouse=True)
def _stub_ban_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every upload test to "hotkey not banned".

    The ban query is unit-tested for real against SQLite in
    ``ditto.tests.db.queries.test_bans``; here we stub it so the endpoint
    tests need no bans row. Ban-specific tests re-stub this to ``True``.
    """
    monkeypatch.setattr(
        "ditto.api_server.endpoints.upload.is_hotkey_banned",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "ditto.api_server.endpoints.upload.get_submission_retry_at",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "ditto.api_server.endpoints.upload.get_upload_admission_for_coldkey",
        AsyncMock(return_value=None),
    )

    async def _settings(_session, *, default_payment_address: str, **_kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            revision=0,
            cooldown_seconds=3600,
            fee_amount_rao=40_000_000,
            payment_address=default_payment_address,
            quotable=True,
        )

    monkeypatch.setattr(
        "ditto.api_server.endpoints.upload.effective_submission_settings",
        AsyncMock(side_effect=_settings),
    )
    monkeypatch.setattr(
        "ditto.api_server.endpoints.upload.consume_or_enforce_upload_admission",
        AsyncMock(return_value=None),
    )


_GOOD_SHA256 = "1d8a3b6f04e2c7f9a51bd3e5c8f2a7b06d4e9c1f2a3b4c5d6e7f8a9b0c1d2e3f"
_BAD_SIG = "a" * 128  # 64 bytes of 0xaa; valid hex but won't verify


def _make_keypair() -> bittensor.Keypair:
    """Deterministic test keypair via the well-known //Alice URI."""
    return bittensor.Keypair.create_from_uri("//Alice")


def _signed_upload_auth(
    keypair: bittensor.Keypair,
    *,
    hotkey: str,
    sha256: str,
    signature_timestamp: int | None = None,
    signature_nonce: UUID | None = None,
) -> dict[str, object]:
    timestamp = (
        signature_timestamp if signature_timestamp is not None else int(time.time())
    )
    nonce = signature_nonce or uuid4()
    payload = (f"ditto-upload-v2:{hotkey}:{sha256}:{timestamp}:{nonce}").encode("ascii")
    return {
        "signature": keypair.sign(payload).hex(),
        "signature_timestamp": timestamp,
        "signature_nonce": str(nonce),
    }


def _signed_request_body(
    *,
    keypair: bittensor.Keypair | None = None,
    sha256: str = _GOOD_SHA256,
    file_size_bytes: int = 1_000_000,
    override_hotkey: str | None = None,
) -> dict[str, object]:
    kp = keypair or _make_keypair()
    hotkey = override_hotkey or kp.ss58_address
    return {
        "hotkey": hotkey,
        "sha256": sha256,
        "file_size_bytes": file_size_bytes,
        **_signed_upload_auth(kp, hotkey=hotkey, sha256=sha256),
    }


class TestEvalPricing:
    @pytest.fixture(autouse=True)
    def _session(self, app: FastAPI) -> None:
        override_get_session(app)

    async def test_happy_path(self, app: FastAPI, client: httpx.AsyncClient):
        override_get_price_oracle(app, price_usd=Decimal("400"))
        response = await client.get("/api/v1/upload/eval-pricing")
        assert response.status_code == 200
        body = response.json()
        assert body["amount_rao"] == 40_000_000
        assert body["send_address"].startswith("5")

    async def test_oracle_down_does_not_block_tao_quote(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        override_get_price_oracle(app, raises=OracleUnreachableError("down"))
        response = await client.get("/api/v1/upload/eval-pricing")
        assert response.status_code == 200
        assert response.json()["amount_rao"] == 40_000_000

    async def test_oracle_stale_does_not_block_tao_quote(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        override_get_price_oracle(app, raises=PriceTooStaleError("stale"))
        response = await client.get("/api/v1/upload/eval-pricing")
        assert response.status_code == 200
        assert response.json()["amount_rao"] == 40_000_000

    async def test_extreme_usd_price_does_not_change_tao_quote(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        # An absurdly high TAO/USD price would make the rao math truncate to 0.
        from decimal import Decimal

        override_get_price_oracle(app, price_usd=Decimal("1e30"))
        response = await client.get("/api/v1/upload/eval-pricing")
        assert response.status_code == 200
        assert response.json()["amount_rao"] == 40_000_000

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
    @pytest.fixture(autouse=True)
    def _session(self, app: FastAPI) -> None:
        # /upload/check now reads the ban list, so it needs a session dep.
        override_get_session(app)
        _override_payment_verifier(app)

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
        assert result["payment_required"] is True

    async def test_reserves_slot_before_payment_when_requested(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        override_get_chain_client(app)
        token = uuid4()
        expires_at = datetime(2026, 7, 24, 20, 30, tzinfo=UTC)
        reserve = AsyncMock(
            return_value=SimpleNamespace(
                token=token,
                expires_at=expires_at,
                cooldown_seconds=3600,
                fee_amount_rao=40_000_000,
                payment_send_address=_make_keypair().ss58_address,
            )
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.reserve_upload_admission", reserve
        )
        response = await client.post(
            "/api/v1/upload/check",
            json={**_signed_request_body(), "reserve_submission_slot": True},
        )

        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert result["admission_token"] == str(token)
        assert result["admission_expires_at"] == "2026-07-24T20:30:00Z"
        assert result["cooldown_seconds"] == 3600
        assert result["payment_amount_rao"] == 40_000_000
        assert result["payment_send_address"]
        reserve.assert_awaited_once()

    async def test_unconsumed_payment_reassigns_same_hotkey_reservation(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        override_get_chain_client(app)
        token = uuid4()
        reserve = AsyncMock(
            return_value=SimpleNamespace(
                token=token,
                expires_at=datetime(2026, 7, 25, 20, 30, tzinfo=UTC),
                cooldown_seconds=3600,
                fee_amount_rao=40_000_000,
                payment_send_address=_make_keypair().ss58_address,
            )
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.reserve_upload_admission", reserve
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_evaluation_payment_for_proof",
            AsyncMock(
                return_value=SimpleNamespace(
                    agent_id=None,
                    miner_hotkey=_make_keypair().ss58_address,
                    miner_coldkey="5Coldkey",
                    timestamp=datetime.now(UTC),
                )
            ),
        )
        response = await client.post(
            "/api/v1/upload/check",
            json={
                **_signed_request_body(sha256="b" * 64),
                "reserve_submission_slot": True,
                "payment_block_hash": _GOOD_BLOCK_HASH,
                "payment_block_number": 13579,
                "payment_extrinsic_index": 7,
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert response.json()["payment_required"] is False
        assert response.json()["admission_token"] == str(token)
        assert reserve.await_args is not None
        assert reserve.await_args.kwargs["replace_existing"] is True

    async def test_assigned_payment_allows_exact_retry_to_reach_upload_agent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        override_get_chain_client(app)
        verifier = _override_payment_verifier(app)
        agent_id = uuid4()
        token = uuid4()
        paid_at = datetime.now(UTC) - timedelta(minutes=50)
        duplicate_lookup = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            duplicate_lookup,
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_evaluation_payment_for_proof",
            AsyncMock(
                return_value=SimpleNamespace(
                    agent_id=agent_id,
                    miner_hotkey=_make_keypair().ss58_address,
                    timestamp=paid_at,
                )
            ),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_agent_for_payment_proof",
            AsyncMock(
                return_value=SimpleNamespace(
                    agent_id=agent_id,
                    miner_hotkey=_make_keypair().ss58_address,
                    sha256=_GOOD_SHA256,
                )
            ),
        )
        reserve = AsyncMock(
            return_value=SimpleNamespace(
                token=token,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                cooldown_seconds=3600,
                fee_amount_rao=40_000_000,
                payment_send_address=_make_keypair().ss58_address,
            )
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.reserve_upload_admission", reserve
        )

        response = await client.post(
            "/api/v1/upload/check",
            json={
                **_signed_request_body(),
                "reserve_submission_slot": True,
                "payment_block_hash": _GOOD_BLOCK_HASH,
                "payment_block_number": 13579,
                "payment_extrinsic_index": 7,
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert response.json()["payment_required"] is False
        assert response.json()["admission_token"] == str(token)
        duplicate_lookup.assert_not_awaited()
        verifier.verify_payment.assert_not_awaited()
        # A consumed proof reserves like a fresh request: no rotation and no
        # reserved-fee recovery (its block time keeps nothing alive).
        assert reserve.await_args is not None
        assert reserve.await_args.kwargs["replace_existing"] is False
        assert reserve.await_args.kwargs["paid_at"] is None

    async def test_payment_older_than_recovery_window_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        override_get_chain_client(app)
        kp = _make_keypair()
        _override_payment_verifier(
            app,
            verified=_make_verified_payment(
                miner_hotkey=kp.ss58_address,
                block_timestamp=datetime.now(UTC) - timedelta(hours=24, seconds=1),
            ),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_evaluation_payment_for_proof",
            AsyncMock(return_value=None),
        )

        response = await client.post(
            "/api/v1/upload/check",
            json={
                **_signed_request_body(keypair=kp),
                "payment_block_hash": _GOOD_BLOCK_HASH,
                "payment_block_number": 13579,
                "payment_extrinsic_index": 7,
            },
        )

        assert response.status_code == 402
        assert response.json()["error_code"] == ERROR_CODE_PAYMENT_RECOVERY_EXPIRED

    async def test_recovery_snapshots_reservation_before_session_rollback(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Chain verification must not lazy-load an expired ORM reservation."""
        from ditto.api_server.dependencies import get_session

        override_get_chain_client(app)
        rolled_back = False
        cutoff = datetime.now(UTC)

        class Reservation:
            miner_hotkey = _make_keypair().ss58_address
            expires_at = datetime.now(UTC) + timedelta(hours=1)
            fee_amount_rao = 40_000_000
            payment_send_address = _make_keypair().ss58_address

            @property
            def legacy_payment_cutoff_at(self) -> datetime:
                if rolled_back:
                    raise RuntimeError("expired ORM attribute was accessed")
                return cutoff

        session = MagicMock()
        session.in_transaction.return_value = True

        async def _rollback() -> None:
            nonlocal rolled_back
            rolled_back = True

        session.rollback = AsyncMock(side_effect=_rollback)

        async def _session_override():  # type: ignore[no-untyped-def]
            yield session

        app.dependency_overrides[get_session] = _session_override
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_upload_admission_for_coldkey",
            AsyncMock(return_value=Reservation()),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_evaluation_payment_for_proof",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )
        verifier = _override_payment_verifier(app)

        response = await client.post(
            "/api/v1/upload/check",
            json={
                **_signed_request_body(),
                "payment_block_hash": _GOOD_BLOCK_HASH,
                "payment_block_number": 13579,
                "payment_extrinsic_index": 7,
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["payment_required"] is False
        assert rolled_back is True
        assert verifier.verify_payment.await_args is not None
        assert (
            verifier.verify_payment.await_args.kwargs["legacy_amount_cutoff_at"]
            == cutoff
        )

    async def test_partial_recovery_payment_proof_is_rejected(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        override_get_chain_client(app)
        response = await client.post(
            "/api/v1/upload/check",
            json={
                **_signed_request_body(),
                "reserve_submission_slot": True,
                "payment_block_hash": _GOOD_BLOCK_HASH,
            },
        )

        assert response.status_code == 422

    async def test_identical_artifact_stops_payment_unless_rescore_is_explicit(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        override_get_chain_client(app)
        duplicate_id = uuid4()
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(
                return_value=SimpleNamespace(
                    agent_id=duplicate_id,
                    status=AgentStatus.SCORED,
                )
            ),
        )
        body = _signed_request_body()

        blocked = await client.post("/api/v1/upload/check", json=body)
        allowed = await client.post(
            "/api/v1/upload/check",
            json={**body, "allow_identical_rescore": True},
        )

        assert blocked.status_code == 200
        blocked_body = blocked.json()
        assert blocked_body["ok"] is False
        assert ERROR_CODE_IDENTICAL_SUBMISSION in blocked_body["error_codes"]
        assert blocked_body["payment_required"] is False
        assert blocked_body["identical_agent_id"] == str(duplicate_id)
        assert blocked_body["messages"] == [
            "The previous submission cannot be resubmitted. "
            "Please try again after updating."
        ]
        assert allowed.json()["ok"] is True
        assert allowed.json()["payment_required"] is True

    async def test_duplicate_from_another_owner_hotkey_precedes_cooldown(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        override_get_chain_client(app)
        previous_id = uuid4()
        lookup = AsyncMock(
            return_value=SimpleNamespace(
                agent_id=previous_id,
                status=AgentStatus.SCORED,
                miner_hotkey=_make_keypair().ss58_address,
            )
        )
        cooldown = AsyncMock(return_value=datetime.now(UTC) + timedelta(hours=1))
        reserve = AsyncMock()
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha", lookup
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_submission_retry_at", cooldown
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.reserve_upload_admission", reserve
        )
        new_hotkey = bittensor.Keypair.create_from_uri("//Bob")

        response = await client.post(
            "/api/v1/upload/check",
            json={
                **_signed_request_body(keypair=new_hotkey),
                "reserve_submission_slot": True,
            },
        )

        assert response.status_code == 200
        result = response.json()
        assert result["error_codes"] == [ERROR_CODE_IDENTICAL_SUBMISSION]
        assert result["identical_agent_id"] == str(previous_id)
        assert result["payment_required"] is False
        assert result["admission_token"] is None
        assert result["retry_at"] is None
        lookup.assert_awaited_once_with(
            ANY, miner_coldkey="5Coldkey", sha256=_GOOD_SHA256
        )
        cooldown.assert_not_awaited()
        reserve.assert_not_awaited()

    async def test_banned_hotkey_returns_1103(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from ditto.api_server.endpoints.upload import ERROR_CODE_HOTKEY_BANNED

        override_get_chain_client(app)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.is_hotkey_banned",
            AsyncMock(return_value=True),
        )
        body = _signed_request_body()
        response = await client.post("/api/v1/upload/check", json=body)
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is False
        assert ERROR_CODE_HOTKEY_BANNED in result["error_codes"]

    async def test_cooldown_returns_retry_timestamp_before_payment(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        override_get_chain_client(app)
        retry_at = datetime.now(UTC).replace(microsecond=0) + timedelta(
            hours=2, minutes=30
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_submission_retry_at",
            AsyncMock(return_value=retry_at),
        )

        response = await client.post(
            "/api/v1/upload/check", json=_signed_request_body()
        )

        result = response.json()
        assert result["ok"] is False
        assert result["payment_required"] is False
        assert ERROR_CODE_SUBMISSION_COOLDOWN in result["error_codes"]
        assert result["retry_at"] == retry_at.isoformat().replace("+00:00", "Z")
        assert result["messages"] == [
            f"owner coldkey may submit again at {retry_at.isoformat()}. "
            "Please try again in 2 hours and 30 minutes."
        ]

    async def test_reservation_race_includes_the_cooldown_countdown(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        override_get_chain_client(app)
        retry_at = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.reserve_upload_admission",
            AsyncMock(side_effect=SubmissionCooldownError(retry_at)),
        )

        response = await client.post(
            "/api/v1/upload/check",
            json={**_signed_request_body(), "reserve_submission_slot": True},
        )

        assert response.status_code == 200, response.text
        result = response.json()
        assert result["error_codes"] == [ERROR_CODE_SUBMISSION_COOLDOWN]
        assert result["payment_required"] is False
        assert result["admission_token"] is None
        assert retry_at.isoformat() in result["messages"][0]
        assert result["messages"][0].endswith(
            "Please try again in 2 hours and 0 minutes."
        )

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

    @pytest.mark.parametrize("offset_seconds", [-301, 301])
    async def test_signed_request_outside_clock_window_is_rejected(
        self, app: FastAPI, client: httpx.AsyncClient, offset_seconds: int
    ) -> None:
        override_get_chain_client(app)
        keypair = _make_keypair()
        body = _signed_request_body(keypair=keypair)
        body.update(
            _signed_upload_auth(
                keypair,
                hotkey=keypair.ss58_address,
                sha256=_GOOD_SHA256,
                signature_timestamp=int(time.time()) + offset_seconds,
            )
        )
        response = await client.post("/api/v1/upload/check", json=body)
        assert response.status_code == 200
        assert ERROR_CODE_BAD_SIGNATURE in response.json()["error_codes"]

    async def test_nonce_cannot_be_changed_after_signing(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        override_get_chain_client(app)
        body = _signed_request_body()
        body["signature_nonce"] = str(uuid4())
        response = await client.post("/api/v1/upload/check", json=body)
        assert response.status_code == 200
        assert ERROR_CODE_BAD_SIGNATURE in response.json()["error_codes"]

    async def test_legacy_static_signature_is_not_accepted(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        override_get_chain_client(app)
        keypair = _make_keypair()
        body = _signed_request_body(keypair=keypair)
        body["signature"] = keypair.sign(
            f"{keypair.ss58_address}:{_GOOD_SHA256}".encode()
        ).hex()
        response = await client.post("/api/v1/upload/check", json=body)
        assert response.status_code == 200
        assert ERROR_CODE_BAD_SIGNATURE in response.json()["error_codes"]

    async def test_unregistered_hotkey_returns_1101(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        # Switch the chain mock to report not-registered.
        from unittest.mock import AsyncMock, MagicMock

        from ditto.api_server.dependencies import get_chain_client

        async def _fake_chain() -> MagicMock:
            chain = MagicMock()
            chain.is_registered = AsyncMock(return_value=False)
            chain.get_registered_coldkey = AsyncMock(return_value=None)
            return chain

        app.dependency_overrides[get_chain_client] = _fake_chain
        body = _signed_request_body()
        response = await client.post("/api/v1/upload/check", json=body)
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is False
        assert ERROR_CODE_HOTKEY_NOT_REGISTERED in result["error_codes"]
        # A client acting on 1101 must be able to bind to this deployment's
        # own target. Without it, registering falls back to a local guess and
        # can recycle TAO on a subnet this platform never reads.
        chain_config = app.state.config.chain
        assert result["netuid"] == chain_config.netuid
        assert result["subtensor_network"] == chain_config.subtensor_network

    async def test_check_always_reports_its_chain_target(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        """Reported on a registered hotkey too, not only alongside 1101."""
        from unittest.mock import AsyncMock, MagicMock

        from ditto.api_server.dependencies import get_chain_client

        async def _fake_chain() -> MagicMock:
            chain = MagicMock()
            chain.is_registered = AsyncMock(return_value=True)
            chain.get_registered_coldkey = AsyncMock(
                return_value="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
            )
            return chain

        app.dependency_overrides[get_chain_client] = _fake_chain
        body = _signed_request_body()
        response = await client.post("/api/v1/upload/check", json=body)
        assert response.status_code == 200
        result = response.json()
        chain_config = app.state.config.chain
        assert result["netuid"] == chain_config.netuid
        assert result["subtensor_network"] == chain_config.subtensor_network

    async def test_tarball_too_large_returns_1102(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        override_get_chain_client(app)
        body = _signed_request_body(file_size_bytes=MAX_TARBALL_SIZE_BYTES + 1)
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
            chain.get_registered_coldkey = AsyncMock(return_value=None)
            return chain

        app.dependency_overrides[get_chain_client] = _fake_chain
        body = _signed_request_body(file_size_bytes=MAX_TARBALL_SIZE_BYTES + 1)
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
        override_get_session(custom_app)  # /upload/check reads the ban list
        _override_payment_verifier(custom_app)

        recorded: dict[str, int] = {}

        async def _fake_chain() -> MagicMock:
            chain = MagicMock()

            async def _get_registered_coldkey(
                _hotkey: str, *, netuid: int
            ) -> str | None:
                recorded["netuid"] = netuid
                return None

            chain.get_registered_coldkey = AsyncMock(
                side_effect=_get_registered_coldkey
            )
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


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


_DOCKERFILE = b"FROM scratch\n"
_GOOD_TAR_BYTES = _tar_bytes({"Dockerfile": _DOCKERFILE})
_GOOD_TAR_SHA = hashlib.sha256(_GOOD_TAR_BYTES).hexdigest()


def _real_source_tar() -> bytes:
    """A genuine tar.gz with a root Dockerfile and a Rust source file."""
    source = b"fn handle(x: i64) -> i64 {\n    let acc = x + 1;\n    acc * 2\n}\n"
    return _tar_bytes({"Dockerfile": _DOCKERFILE, "src/lib.rs": source})


_GOOD_BLOCK_HASH = "0x" + "ab" * 32


def _make_verified_payment(**overrides: Any) -> VerifiedPayment:
    base: dict[str, Any] = {
        "block_hash": _GOOD_BLOCK_HASH,
        "extrinsic_index": 7,
        "miner_hotkey": "5Hotkey",  # overridden in fixtures
        "miner_coldkey": "5Coldkey",
        "amount_rao": 17_500_000,
        "tao_usd_rate": Decimal("400"),
        "dest_address": "5SendAddress",
        "block_timestamp": datetime.now(UTC),
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
    session.scalar = AsyncMock(return_value=0)
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
    data: dict[str, Any] = {
        "hotkey": hotkey,
        "sha256": sha256,
        "name": name,
        **_signed_upload_auth(kp, hotkey=hotkey, sha256=sha256),
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
    async def test_payment_terms_are_snapshotted_before_session_rollback(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        deps = _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        verifier = _override_payment_verifier(
            app, verified=_make_verified_payment(miner_hotkey=kp.ss58_address)
        )
        rollback_count = 0
        cutoff = datetime.now(UTC)

        class Reservation:
            miner_hotkey = kp.ss58_address
            sha256 = _GOOD_TAR_SHA
            fee_amount_rao = 40_000_000
            payment_send_address = _make_keypair().ss58_address
            expires_at = datetime.now(UTC) + timedelta(hours=1)

            @property
            def legacy_payment_cutoff_at(self) -> datetime:
                if rollback_count >= 2:
                    raise RuntimeError("expired ORM attribute was accessed")
                return cutoff

        async def _rollback() -> None:
            nonlocal rollback_count
            rollback_count += 1

        deps["session"].in_transaction.return_value = True
        deps["session"].rollback = AsyncMock(side_effect=_rollback)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_upload_admission",
            AsyncMock(return_value=Reservation()),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )
        data, files = _upload_agent_form(keypair=kp)
        data["admission_token"] = str(uuid4())

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 200, response.text
        assert rollback_count >= 2
        assert verifier.verify_payment.await_args is not None
        assert (
            verifier.verify_payment.await_args.kwargs["legacy_amount_cutoff_at"]
            == cutoff
        )

    @pytest.mark.parametrize(
        "expires_in", [timedelta(hours=1), timedelta(seconds=-3600)]
    )
    async def test_reserved_quote_terms_are_anchored_on_payment_time(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        expires_in: timedelta,
    ) -> None:
        """The reservation's fee is handed to the verifier with its expiry and
        the current fee, whether or not the reservation has expired by the time
        the upload arrives: the payment's block time picks the terms."""
        _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        verifier = _override_payment_verifier(
            app, verified=_make_verified_payment(miner_hotkey=kp.ss58_address)
        )
        expires_at = datetime.now(UTC) + expires_in
        reserved_address = _make_keypair().ss58_address

        class Reservation:
            miner_hotkey = kp.ss58_address
            sha256 = _GOOD_TAR_SHA
            fee_amount_rao = 40_000_000
            payment_send_address = reserved_address
            legacy_payment_cutoff_at = None

        Reservation.expires_at = expires_at  # type: ignore[attr-defined]

        async def _changed_policy(  # type: ignore[no-untyped-def]
            _session, *, default_payment_address: str, **_kwargs
        ):
            return SimpleNamespace(
                revision=7,
                cooldown_seconds=3600,
                fee_amount_rao=90_000_000,
                payment_address=default_payment_address,
                quotable=True,
            )

        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.effective_submission_settings",
            AsyncMock(side_effect=_changed_policy),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_upload_admission",
            AsyncMock(return_value=Reservation()),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )
        consume = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.consume_or_enforce_upload_admission",
            consume,
        )
        data, files = _upload_agent_form(keypair=kp)
        data["admission_token"] = str(uuid4())

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 200, response.text
        assert verifier.verify_payment.await_args is not None
        kwargs = verifier.verify_payment.await_args.kwargs
        assert kwargs["expected_amount_rao"] == 40_000_000
        assert kwargs["expected_send_address"] == reserved_address
        assert kwargs["reserved_terms_expire_at"] == expires_at
        assert kwargs["fallback_amount_rao"] == 90_000_000
        # Consumption is judged against the payment's block time too.
        assert consume.await_args is not None
        assert consume.await_args.kwargs["paid_at"] is not None

    @pytest.mark.parametrize(
        ("paid_hours_after_quote", "paid_rao", "accepted"),
        [
            # Quote at T0, pay the quoted fee at T0+23h, upload at T0+25h after
            # an operator fee change: the payment predates the quote's expiry,
            # so the reserved fee still binds.
            (23, 40_000_000, True),
            # Paid after the quote expired: the current fee is required...
            (24.5, 90_000_000, True),
            # ...and the old reserved fee is refused.
            (24.5, 40_000_000, False),
            # A payment made in time cannot pay the new fee instead.
            (23, 90_000_000, False),
        ],
    )
    async def test_late_upload_of_in_time_payment_keeps_reserved_fee(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        paid_hours_after_quote: float,
        paid_rao: int,
        accepted: bool,
    ) -> None:
        from ditto.api_server.dependencies import get_payment_verifier
        from ditto.api_server.payment_verifier import PaymentVerifier

        _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        address = app.state.config.upload_payment_address
        upload_at = datetime.now(UTC)
        quoted_at = upload_at - timedelta(hours=25)
        paid_at = quoted_at + timedelta(hours=paid_hours_after_quote)

        chain = MagicMock()
        chain.get_block_hash = AsyncMock(return_value=_GOOD_BLOCK_HASH)
        extrinsic = MagicMock()
        extrinsic.call_module = "Balances"
        extrinsic.call_function = "transfer_keep_alive"
        extrinsic.call_args = {"dest": address, "value": paid_rao}
        extrinsic.signer_address = "5Coldkey"
        chain.get_extrinsic = AsyncMock(return_value=extrinsic)
        chain.check_extrinsic_success = AsyncMock(return_value=True)
        chain.get_block_timestamp = AsyncMock(return_value=int(paid_at.timestamp()))
        chain.get_coldkey_for_hotkey = AsyncMock(return_value="5Coldkey")
        oracle = MagicMock()
        oracle.get_tao_usd = AsyncMock(return_value=Decimal("400"))
        real_verifier = PaymentVerifier(
            chain=chain, oracle=oracle, send_address=address
        )

        async def _verifier() -> PaymentVerifier:
            return real_verifier

        app.dependency_overrides[get_payment_verifier] = _verifier

        class Reservation:
            miner_hotkey = kp.ss58_address
            sha256 = _GOOD_TAR_SHA
            fee_amount_rao = 40_000_000
            payment_send_address = address
            legacy_payment_cutoff_at = None
            expires_at = quoted_at + timedelta(hours=24)

        async def _changed_policy(  # type: ignore[no-untyped-def]
            _session, *, default_payment_address: str, **_kwargs
        ):
            return SimpleNamespace(
                revision=7,
                cooldown_seconds=3600,
                fee_amount_rao=90_000_000,
                payment_address=default_payment_address,
                quotable=True,
            )

        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.effective_submission_settings",
            AsyncMock(side_effect=_changed_policy),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_upload_admission",
            AsyncMock(return_value=Reservation()),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )
        data, files = _upload_agent_form(keypair=kp)
        data["admission_token"] = str(uuid4())

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        if accepted:
            assert response.status_code == 200, response.text
        else:
            assert response.status_code == 402, response.text
            assert response.json()["error_code"] == ERROR_CODE_PAYMENT_AMOUNT_MISMATCH

    @pytest.mark.parametrize("with_quote", [True, False])
    async def test_unquotable_policy_never_refuses_an_issued_quote(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        with_quote: bool,
    ) -> None:
        """An unreviewed-denomination revision refuses new quotes (503) but an
        upload paying a still-bound quote is verified and stored."""
        from ditto.api_server.pricing.errors import UnsupportedFeeDenominationError

        _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        verifier = _override_payment_verifier(
            app, verified=_make_verified_payment(miner_hotkey=kp.ss58_address)
        )
        reads: list[bool] = []

        async def _unquotable(  # type: ignore[no-untyped-def]
            _session, *, default_payment_address: str, require_quotable: bool = True
        ):
            reads.append(require_quotable)
            if require_quotable:
                raise UnsupportedFeeDenominationError("usd_indexed")
            return SimpleNamespace(
                revision=9,
                cooldown_seconds=3600,
                fee_amount_rao=5,
                payment_address=default_payment_address,
                quotable=False,
            )

        class Reservation:
            miner_hotkey = kp.ss58_address
            sha256 = _GOOD_TAR_SHA
            fee_amount_rao = 40_000_000
            payment_send_address = _make_keypair().ss58_address
            legacy_payment_cutoff_at = None
            expires_at = datetime.now(UTC) + timedelta(hours=1)

        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.effective_submission_settings",
            AsyncMock(side_effect=_unquotable),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_upload_admission",
            AsyncMock(return_value=Reservation() if with_quote else None),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )
        data, files = _upload_agent_form(keypair=kp)
        if with_quote:
            data["admission_token"] = str(uuid4())

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        if with_quote:
            assert response.status_code == 200, response.text
            assert reads == [False, False]
            kwargs = verifier.verify_payment.await_args.kwargs
            assert kwargs["expected_amount_rao"] == 40_000_000
            # Without a quotable current fee a post-expiry payment fails closed.
            assert kwargs["fallback_amount_rao"] is None
        else:
            assert response.status_code == 503, response.text
            assert response.json()["error_code"] == 3100
            verifier.verify_payment.assert_not_awaited()

    async def test_identical_paid_upload_returns_reusable_credit(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        deps = _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        _override_payment_verifier(
            app, verified=_make_verified_payment(miner_hotkey=kp.ss58_address)
        )
        duplicate_id = uuid4()
        rollback_count = 0

        class Duplicate:
            @property
            def agent_id(self):  # type: ignore[no-untyped-def]
                if rollback_count >= 3:
                    raise RuntimeError("expired duplicate agent was accessed")
                return duplicate_id

            @property
            def version(self):  # type: ignore[no-untyped-def]
                if rollback_count >= 3:
                    raise RuntimeError("expired duplicate agent was accessed")
                return 2

            @property
            def status(self):  # type: ignore[no-untyped-def]
                if rollback_count >= 3:
                    raise RuntimeError("expired duplicate agent was accessed")
                return AgentStatus.SCORED

        async def _rollback() -> None:
            nonlocal rollback_count
            rollback_count += 1

        deps["session"].in_transaction.return_value = True
        deps["session"].rollback = AsyncMock(side_effect=_rollback)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=Duplicate()),
        )
        data, files = _upload_agent_form(keypair=kp)

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "agent_id": str(duplicate_id),
            "version": 2,
            "status": "scored",
            "payment_disposition": "reusable_credit",
            "credit_for_agent_id": str(duplicate_id),
        }
        deps["storage"].put_object.assert_not_awaited()
        credit_row = deps["session"].add.call_args.args[0]
        assert credit_row.agent_id is None
        assert credit_row.credit_for_agent_id == duplicate_id
        assert rollback_count >= 3

    async def test_available_credit_funds_a_different_artifact(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        deps = _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        credit = SimpleNamespace(
            agent_id=None,
            credit_for_agent_id=uuid4(),
            miner_hotkey=kp.ss58_address,
            miner_coldkey="5Coldkey",
            timestamp=datetime.now(UTC),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_evaluation_payment_for_proof",
            AsyncMock(side_effect=[credit, credit]),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )
        data, files = _upload_agent_form(keypair=kp, sha256=_GOOD_TAR_SHA)

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 200, response.text
        assert response.json()["payment_disposition"] == "credit_consumed"
        deps["verifier"].verify_payment.assert_not_awaited()
        deps["storage"].put_object.assert_awaited_once()
        assert str(credit.agent_id) == response.json()["agent_id"]
        assert credit.credit_for_agent_id is None

    async def test_expired_credit_cannot_fund_a_different_artifact(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        deps = _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        credit = SimpleNamespace(
            agent_id=None,
            credit_for_agent_id=uuid4(),
            miner_hotkey=kp.ss58_address,
            miner_coldkey="5Coldkey",
            timestamp=datetime.now(UTC) - timedelta(hours=24, seconds=1),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_evaluation_payment_for_proof",
            AsyncMock(return_value=credit),
        )
        data, files = _upload_agent_form(keypair=kp, sha256=_GOOD_TAR_SHA)

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 402
        assert response.json()["error_code"] == ERROR_CODE_PAYMENT_RECOVERY_EXPIRED
        deps["verifier"].verify_payment.assert_not_awaited()
        deps["storage"].put_object.assert_not_awaited()

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
        assert body["version"] == 1
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

    async def test_rejects_a_non_archive_before_payment_or_storage(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        deps = _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        junk = b"\x1f\x8b" + b"x" * 64
        data, _ = _upload_agent_form(
            keypair=kp, sha256=hashlib.sha256(junk).hexdigest()
        )
        files = {"agent_tar": ("harness.tar.gz", junk, "application/gzip")}

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 400
        assert "gzip-compressed tar" in response.json()["message"]
        deps["verifier"].verify_payment.assert_not_awaited()
        deps["storage"].put_object.assert_not_awaited()

    async def test_stores_code_embedding_when_enabled(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        # With an enabled embedder and a real-source tar, the code-embedding vector +
        # model tag
        # reach the agent row (shadow storage).
        deps = _wire_full_stack(app)
        override_get_embedder(app, vector=[0.1, 0.2, 0.3])
        kp = bittensor.Keypair.create_from_uri("//Alice")
        _override_payment_verifier(
            app, verified=_make_verified_payment(miner_hotkey=kp.ss58_address)
        )
        tar = _real_source_tar()
        data, _ = _upload_agent_form(keypair=kp, sha256=hashlib.sha256(tar).hexdigest())
        files = {"agent_tar": ("harness.tar.gz", tar, "application/gzip")}

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 200, response.text
        agent_row = deps["session"].add.call_args_list[0].args[0]
        assert agent_row.code_embedding == [0.1, 0.2, 0.3]
        assert agent_row.code_embed_model == "stub@test"

    async def test_disabled_embedder_leaves_embedding_null(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        # Default null embedder: even a real-source tar stores no vector.
        deps = _wire_full_stack(app)  # app fixture defaults to the null embedder
        kp = bittensor.Keypair.create_from_uri("//Alice")
        _override_payment_verifier(
            app, verified=_make_verified_payment(miner_hotkey=kp.ss58_address)
        )
        tar = _real_source_tar()
        data, _ = _upload_agent_form(keypair=kp, sha256=hashlib.sha256(tar).hexdigest())
        files = {"agent_tar": ("harness.tar.gz", tar, "application/gzip")}

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 200, response.text
        agent_row = deps["session"].add.call_args_list[0].args[0]
        assert agent_row.code_embedding is None
        assert agent_row.code_embed_model is None


class TestUploadAgentValidationFailures:
    async def test_cooldown_race_returns_429_with_retry_after(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        _override_payment_verifier(
            app, verified=_make_verified_payment(miner_hotkey=kp.ss58_address)
        )
        retry_at = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=30)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.consume_or_enforce_upload_admission",
            AsyncMock(side_effect=SubmissionCooldownError(retry_at)),
        )
        data, files = _upload_agent_form(keypair=kp)

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) in range(1798, 1801)
        assert retry_at.isoformat() in response.json()["message"]
        assert response.json()["message"].endswith(
            "Please try again in 0 hours and 30 minutes."
        )

    async def test_bad_signature_returns_400(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        _wire_full_stack(app)
        data, files = _upload_agent_form()
        data["signature"] = "a" * 128  # valid hex shape, wrong sig

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 400
        assert "signature" in response.json()["message"]

    async def test_signed_but_expired_upload_is_rejected_before_payment_work(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        deps = _wire_full_stack(app)
        keypair = _make_keypair()
        data, files = _upload_agent_form(keypair=keypair)
        data.update(
            _signed_upload_auth(
                keypair,
                hotkey=keypair.ss58_address,
                sha256=_GOOD_TAR_SHA,
                signature_timestamp=int(time.time()) - 301,
            )
        )
        response = await client.post("/api/v1/upload/agent", data=data, files=files)
        assert response.status_code == 400
        deps["verifier"].verify_payment.assert_not_awaited()

    async def test_banned_hotkey_returns_403(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # A valid signature (so the ban check is reached) from a banned hotkey
        # is rejected 403 before any chain/payment/storage work.
        _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.is_hotkey_banned",
            AsyncMock(return_value=True),
        )
        data, files = _upload_agent_form(keypair=kp)

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 403
        assert "banned" in response.json()["message"]

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
        data = {
            "hotkey": kp.ss58_address,
            "sha256": bogus_sha,
            "name": "alpha-agent",
            **_signed_upload_auth(kp, hotkey=kp.ss58_address, sha256=bogus_sha),
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
        oversized = b"\x1f\x8b" + b"x" * (MAX_TARBALL_SIZE_BYTES - 1)
        big_sha = hashlib.sha256(oversized).hexdigest()
        kp = bittensor.Keypair.create_from_uri("//Alice")
        data = {
            "hotkey": kp.ss58_address,
            "sha256": big_sha,
            "name": "alpha-agent",
            **_signed_upload_auth(kp, hotkey=kp.ss58_address, sha256=big_sha),
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
                PaymentRecoveryExpired("expired"),
                ERROR_CODE_PAYMENT_RECOVERY_EXPIRED,
            ),
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
    async def test_exact_retry_returns_original_without_reprocessing(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        deps = _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        original_id = uuid4()
        admission_token = uuid4()
        release = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.release_upload_admission_for_exact_retry",
            release,
        )
        deps["session"].commit = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_agent_for_payment_proof",
            AsyncMock(
                return_value=SimpleNamespace(
                    agent_id=original_id,
                    miner_hotkey=kp.ss58_address,
                    name="alpha-agent",
                    sha256=_GOOD_TAR_SHA,
                    version=3,
                    status=AgentStatus.SCREENING,
                )
            ),
        )
        data, files = _upload_agent_form(keypair=kp)
        data["admission_token"] = str(admission_token)

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "agent_id": str(original_id),
            "version": 3,
            "status": "screening",
        }
        deps["verifier"].verify_payment.assert_not_awaited()
        release.assert_awaited_once_with(
            deps["session"],
            token=admission_token,
            miner_hotkey=kp.ss58_address,
            sha256=_GOOD_TAR_SHA,
        )
        deps["session"].commit.assert_awaited_once()
        deps["storage"].put_object.assert_not_awaited()

    async def test_reused_proof_for_different_upload_stays_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _wire_full_stack(app)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_agent_for_payment_proof",
            AsyncMock(
                return_value=SimpleNamespace(
                    agent_id=uuid4(),
                    miner_hotkey="5DifferentHotkey1111111111111111111111111111111",
                    name="other-agent",
                    sha256="ff" * 32,
                    version=1,
                    status=AgentStatus.UPLOADED,
                )
            ),
        )
        data, files = _upload_agent_form()

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 402
        assert response.json()["error_code"] == ERROR_CODE_PAYMENT_REPLAYED

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


class TestUploadAgentBoundaries:
    """Pin boundary values whose off-by-one regressions would silently
    reject legitimate uploads or accept malformed ones."""

    async def test_size_exactly_at_cap_accepted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """``_read_tar_capped_with_sha`` uses ``size > max_bytes``; this
        test pins the boundary so a refactor to ``>=`` is caught."""
        _wire_full_stack(app)
        at_cap = _GOOD_TAR_BYTES
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.MAX_TARBALL_SIZE_BYTES",
            len(at_cap),
        )
        at_cap_sha = hashlib.sha256(at_cap).hexdigest()
        kp = bittensor.Keypair.create_from_uri("//Alice")
        _override_payment_verifier(
            app, verified=_make_verified_payment(miner_hotkey=kp.ss58_address)
        )
        data = {
            "hotkey": kp.ss58_address,
            "sha256": at_cap_sha,
            "name": "alpha-agent",
            **_signed_upload_auth(kp, hotkey=kp.ss58_address, sha256=at_cap_sha),
            "payment_block_hash": _GOOD_BLOCK_HASH,
            "payment_block_number": 13579,
            "payment_extrinsic_index": 7,
        }
        files = {"agent_tar": ("harness.tar.gz", at_cap, "application/gzip")}

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 200, response.text

    async def test_size_one_over_cap_rejected(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        _wire_full_stack(app)
        over = b"\x1f\x8b" + b"x" * (MAX_TARBALL_SIZE_BYTES - 2) + b"!"
        over_sha = hashlib.sha256(over).hexdigest()
        kp = bittensor.Keypair.create_from_uri("//Alice")
        data = {
            "hotkey": kp.ss58_address,
            "sha256": over_sha,
            "name": "alpha-agent",
            **_signed_upload_auth(kp, hotkey=kp.ss58_address, sha256=over_sha),
            "payment_block_hash": _GOOD_BLOCK_HASH,
            "payment_block_number": 13579,
            "payment_extrinsic_index": 7,
        }
        files = {"agent_tar": ("harness.tar.gz", over, "application/gzip")}

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 413

    async def test_extrinsic_index_zero_accepted(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        """``payment_extrinsic_index`` uses ``Form(ge=0)``; index 0 is
        the first extrinsic in any block and must be valid."""
        _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        _override_payment_verifier(
            app, verified=_make_verified_payment(miner_hotkey=kp.ss58_address)
        )
        data, files = _upload_agent_form(keypair=kp)
        data["payment_extrinsic_index"] = 0

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 200, response.text

    async def test_payment_block_number_zero_rejected(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        """``payment_block_number`` uses ``Form(ge=1)``; 0 must 422."""
        _wire_full_stack(app)
        data, files = _upload_agent_form()
        data["payment_block_number"] = 0

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 422

    async def test_name_at_max_length_accepted(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        """The 64-char ``name`` cap is a chosen value, not spec-mandated;
        pin both ends so a future tightening is a deliberate change."""
        _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        _override_payment_verifier(
            app, verified=_make_verified_payment(miner_hotkey=kp.ss58_address)
        )
        data, files = _upload_agent_form(keypair=kp, name="x" * 64)

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 200, response.text

    async def test_name_over_max_length_rejected(
        self, app: FastAPI, client: httpx.AsyncClient
    ):
        _wire_full_stack(app)
        data, files = _upload_agent_form(name="x" * 65)

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 422

    async def test_name_empty_rejected(self, app: FastAPI, client: httpx.AsyncClient):
        _wire_full_stack(app)
        data, files = _upload_agent_form(name="")

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 422


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


class TestUploadReleasesSessionDuringSlowWork:
    async def test_no_transaction_held_across_tarball_read(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        session_maker: async_sessionmaker[AsyncSession],
    ):
        """The pooled session must hold NO transaction while the tarball streams.

        The ban check autobegins a transaction; the endpoint must end it before
        the slow middle (tarball read, payment verify, storage write,
        fingerprinting), or concurrent slow uploads pin every pool slot
        (the 2026-07-16 production outage).
        """
        from ditto.api_server.dependencies import get_session
        from ditto.api_server.endpoints import upload as upload_mod
        from ditto.db.queries.bans import is_hotkey_banned as real_is_hotkey_banned

        maker = session_maker
        session_holder: dict[str, Any] = {}

        async def _real_session():
            async with maker() as s:
                session_holder["session"] = s
                yield s

        app.dependency_overrides[get_session] = _real_session
        # The autouse fixture stubs the ban check; restore the real query so the
        # session autobegins exactly as in production.
        monkeypatch.setattr(upload_mod, "is_hotkey_banned", real_is_hotkey_banned)

        override_get_storage_client(app)
        override_get_chain_client(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        _override_payment_verifier(
            app, verified=_make_verified_payment(miner_hotkey=kp.ss58_address)
        )

        real_read = upload_mod._read_tar_capped_with_sha
        seen: dict[str, Any] = {}

        async def _spy(file: Any, max_bytes: int):
            seen["in_transaction"] = session_holder["session"].in_transaction()
            return await real_read(file, max_bytes)

        monkeypatch.setattr(upload_mod, "_read_tar_capped_with_sha", _spy)

        data, files = _upload_agent_form(keypair=kp)
        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 200, response.text
        assert seen["in_transaction"] is False


def _real_verifier_paid_at(
    app: FastAPI, *, paid_at: datetime, paid_rao: int, address: str
) -> None:
    """Install the real PaymentVerifier over a fake chain paying ``paid_rao``."""
    from ditto.api_server.dependencies import get_payment_verifier
    from ditto.api_server.payment_verifier import PaymentVerifier

    chain = MagicMock()
    chain.get_block_hash = AsyncMock(return_value=_GOOD_BLOCK_HASH)
    extrinsic = MagicMock()
    extrinsic.call_module = "Balances"
    extrinsic.call_function = "transfer_keep_alive"
    extrinsic.call_args = {"dest": address, "value": paid_rao}
    extrinsic.signer_address = "5Coldkey"
    chain.get_extrinsic = AsyncMock(return_value=extrinsic)
    chain.check_extrinsic_success = AsyncMock(return_value=True)
    chain.get_block_timestamp = AsyncMock(return_value=int(paid_at.timestamp()))
    chain.get_coldkey_for_hotkey = AsyncMock(return_value="5Coldkey")
    oracle = MagicMock()
    oracle.get_tao_usd = AsyncMock(return_value=Decimal("400"))
    verifier = PaymentVerifier(chain=chain, oracle=oracle, send_address=address)

    async def _verifier() -> PaymentVerifier:
        return verifier

    app.dependency_overrides[get_payment_verifier] = _verifier


def _unquotable_policy(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Current revision is in an unreviewed denomination; record read modes."""
    from ditto.api_server.pricing.errors import UnsupportedFeeDenominationError

    reads: list[bool] = []

    async def _settings(  # type: ignore[no-untyped-def]
        _session, *, default_payment_address: str, require_quotable: bool = True
    ):
        reads.append(require_quotable)
        if require_quotable:
            raise UnsupportedFeeDenominationError("usd_indexed")
        return SimpleNamespace(
            revision=9,
            cooldown_seconds=3600,
            fee_amount_rao=5,
            payment_address=default_payment_address,
            quotable=False,
        )

    monkeypatch.setattr(
        "ditto.api_server.endpoints.upload.effective_submission_settings",
        AsyncMock(side_effect=_settings),
    )
    return reads


class TestReservedQuoteUnderUnquotablePricing:
    """/upload/check recovery and /upload/agent honour an issued quote while the
    current revision cannot be quoted, and stay fail-closed outside it."""

    @pytest.mark.parametrize(
        ("with_reservation", "paid_hours_after_quote", "expected_status"),
        [
            # Paid inside the reservation: the reserved fee binds -> recovered.
            (True, 23, 200),
            # Paid after the reservation expired: no current fee can be quoted.
            (True, 24.5, 503),
            # No reservation at all: nothing can be quoted.
            (False, 23, 503),
        ],
    )
    async def test_check_recovery(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        with_reservation: bool,
        paid_hours_after_quote: float,
        expected_status: int,
    ) -> None:
        override_get_session(app)
        override_get_chain_client(app)
        address = app.state.config.upload_payment_address
        quoted_at = datetime.now(UTC) - timedelta(hours=25)
        _real_verifier_paid_at(
            app,
            paid_at=quoted_at + timedelta(hours=paid_hours_after_quote),
            paid_rao=40_000_000,
            address=address,
        )
        reads = _unquotable_policy(monkeypatch)

        class Reservation:
            miner_hotkey = _make_keypair().ss58_address
            fee_amount_rao = 40_000_000
            payment_send_address = address
            legacy_payment_cutoff_at = None
            expires_at = quoted_at + timedelta(hours=24)

        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_upload_admission_for_coldkey",
            AsyncMock(return_value=Reservation() if with_reservation else None),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_evaluation_payment_for_proof",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )

        response = await client.post(
            "/api/v1/upload/check",
            json={
                **_signed_request_body(),
                "payment_block_hash": _GOOD_BLOCK_HASH,
                "payment_block_number": 13579,
                "payment_extrinsic_index": 7,
            },
        )

        assert response.status_code == expected_status, response.text
        if expected_status == 200:
            assert response.json()["payment_required"] is False
            assert reads[0] is False
        else:
            assert response.json()["error_code"] == 3100

    @pytest.mark.parametrize(
        ("paid_hours_after_quote", "expected_status"), [(23, 200), (24.5, 503)]
    )
    async def test_agent_upload(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        paid_hours_after_quote: float,
        expected_status: int,
    ) -> None:
        _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        address = app.state.config.upload_payment_address
        quoted_at = datetime.now(UTC) - timedelta(hours=25)
        _real_verifier_paid_at(
            app,
            paid_at=quoted_at + timedelta(hours=paid_hours_after_quote),
            paid_rao=40_000_000,
            address=address,
        )
        _unquotable_policy(monkeypatch)

        class Reservation:
            miner_hotkey = kp.ss58_address
            sha256 = _GOOD_TAR_SHA
            fee_amount_rao = 40_000_000
            payment_send_address = address
            legacy_payment_cutoff_at = None
            expires_at = quoted_at + timedelta(hours=24)

        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_upload_admission",
            AsyncMock(return_value=Reservation()),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )
        data, files = _upload_agent_form(keypair=kp)
        data["admission_token"] = str(uuid4())

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == expected_status, response.text
        if expected_status == 503:
            assert response.json()["error_code"] == 3100


class TestKeptReservationUnderUnquotablePricing:
    """Recovery with ``reserve_submission_slot`` keeps an expired reservation
    that was live when its payment finalized, under an unquotable revision."""

    @pytest.mark.parametrize("same_archive", [True, False])
    async def test_recovery_reserve_keeps_reserved_fee_and_expiry(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        session_maker: async_sessionmaker[AsyncSession],
        same_archive: bool,
    ) -> None:
        from ditto.api_server.dependencies import get_session
        from ditto.db.models import UploadAdmissionReservation
        from ditto.db.queries import submission_settings as queries

        async def _session():  # type: ignore[no-untyped-def]
            async with session_maker() as session:
                yield session

        app.dependency_overrides[get_session] = _session
        override_get_chain_client(app)
        address = app.state.config.upload_payment_address
        hotkey = _make_keypair().ss58_address
        quoted_at = datetime.now(UTC) - timedelta(hours=25)
        async with session_maker() as session, session.begin():
            issued = await queries.reserve_upload_admission(
                session,
                miner_coldkey="5Coldkey",
                miner_hotkey=hotkey,
                sha256=_GOOD_SHA256 if same_archive else "f" * 64,
                settings=queries.EffectiveSubmissionSettings(
                    revision=1,
                    cooldown_seconds=3600,
                    payment_address=address,
                    fee_amount_rao=40_000_000,
                ),
                now=quoted_at,
            )
        _real_verifier_paid_at(
            app,
            paid_at=quoted_at + timedelta(hours=23),
            paid_rao=40_000_000,
            address=address,
        )
        _unquotable_policy(monkeypatch)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_upload_admission_for_coldkey",
            queries.get_upload_admission_for_coldkey,
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )

        response = await client.post(
            "/api/v1/upload/check",
            json={
                **_signed_request_body(),
                "reserve_submission_slot": True,
                "payment_block_hash": _GOOD_BLOCK_HASH,
                "payment_block_number": 13579,
                "payment_extrinsic_index": 7,
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["payment_required"] is False
        async with session_maker() as session:
            kept = await session.get(UploadAdmissionReservation, "5Coldkey")
        assert kept is not None
        assert kept.fee_amount_rao == 40_000_000
        assert kept.expires_at == issued.expires_at
        assert kept.sha256 == _GOOD_SHA256
        assert body["admission_token"] == str(kept.token)
        assert (kept.token == issued.token) is same_archive

    async def test_recorded_credit_recovery_keeps_reserved_fee_and_expiry(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Recovering a recorded, unused credit forwards its recorded block
        time, like a chain-verified payment does."""
        from ditto.api_server.dependencies import get_session
        from ditto.db.models import UploadAdmissionReservation
        from ditto.db.queries import submission_settings as queries

        async def _session():  # type: ignore[no-untyped-def]
            async with session_maker() as session:
                yield session

        app.dependency_overrides[get_session] = _session
        override_get_chain_client(app)
        verifier = _override_payment_verifier(app)
        address = app.state.config.upload_payment_address
        hotkey = _make_keypair().ss58_address
        quoted_at = datetime.now(UTC) - timedelta(hours=25)
        async with session_maker() as session, session.begin():
            issued = await queries.reserve_upload_admission(
                session,
                miner_coldkey="5Coldkey",
                miner_hotkey=hotkey,
                sha256=_GOOD_SHA256,
                settings=queries.EffectiveSubmissionSettings(
                    revision=1,
                    cooldown_seconds=3600,
                    payment_address=address,
                    fee_amount_rao=40_000_000,
                ),
                now=quoted_at,
            )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_evaluation_payment_for_proof",
            AsyncMock(
                return_value=SimpleNamespace(
                    agent_id=None,
                    miner_hotkey=hotkey,
                    miner_coldkey="5Coldkey",
                    timestamp=quoted_at + timedelta(hours=23),
                )
            ),
        )
        _unquotable_policy(monkeypatch)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_upload_admission_for_coldkey",
            queries.get_upload_admission_for_coldkey,
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )

        response = await client.post(
            "/api/v1/upload/check",
            json={
                **_signed_request_body(),
                "reserve_submission_slot": True,
                "payment_block_hash": _GOOD_BLOCK_HASH,
                "payment_block_number": 13579,
                "payment_extrinsic_index": 7,
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["payment_required"] is False
        verifier.verify_payment.assert_not_awaited()
        async with session_maker() as session:
            kept = await session.get(UploadAdmissionReservation, "5Coldkey")
        assert kept is not None
        assert kept.token == issued.token
        assert kept.fee_amount_rao == 40_000_000
        assert kept.expires_at == issued.expires_at
        assert body["admission_token"] == str(kept.token)


class TestReservationSendAddressIsSingleSourced:
    async def test_legacy_null_address_verifies_against_the_advertised_one(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A legacy reservation without a stored destination is verified against
        the current effective deposit address, the same one reservations
        advertise, not the static config default."""
        _wire_full_stack(app)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        verifier = _override_payment_verifier(
            app, verified=_make_verified_payment(miner_hotkey=kp.ss58_address)
        )
        effective = "5RotatedEffectiveDepositAddress"
        assert effective != app.state.config.upload_payment_address

        async def _rotated(_session, **_kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                revision=1,
                cooldown_seconds=3600,
                fee_amount_rao=40_000_000,
                payment_address=effective,
                quotable=True,
            )

        class Reservation:
            miner_hotkey = kp.ss58_address
            sha256 = _GOOD_TAR_SHA
            fee_amount_rao = 40_000_000
            payment_send_address = None
            legacy_payment_cutoff_at = None
            expires_at = datetime.now(UTC) + timedelta(hours=1)

        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.effective_submission_settings",
            AsyncMock(side_effect=_rotated),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_upload_admission",
            AsyncMock(return_value=Reservation()),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )
        data, files = _upload_agent_form(keypair=kp)
        data["admission_token"] = str(uuid4())

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 200, response.text
        assert verifier.verify_payment.await_args is not None
        assert (
            verifier.verify_payment.await_args.kwargs["expected_send_address"]
            == effective
        )

    async def test_check_advertises_the_effective_address_for_a_null_row(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """/upload/check never returns a legacy NULL destination: the quote it
        advertises comes from the same helper the verifier uses."""
        from ditto.api_server.dependencies import get_session
        from ditto.db.models import UploadAdmissionReservation
        from ditto.db.queries import submission_settings as queries

        async def _session():  # type: ignore[no-untyped-def]
            async with session_maker() as session:
                yield session

        app.dependency_overrides[get_session] = _session
        override_get_chain_client(app)
        _override_payment_verifier(app)
        hotkey = _make_keypair().ss58_address
        effective = bittensor.Keypair.create_from_uri("//Dave").ss58_address
        assert effective != app.state.config.upload_payment_address
        async with session_maker() as session, session.begin():
            issued = await queries.reserve_upload_admission(
                session,
                miner_coldkey="5Coldkey",
                miner_hotkey=hotkey,
                sha256=_GOOD_SHA256,
                settings=queries.EffectiveSubmissionSettings(
                    revision=1,
                    cooldown_seconds=3600,
                    payment_address=effective,
                    fee_amount_rao=40_000_000,
                ),
            )
            legacy = await session.get(UploadAdmissionReservation, "5Coldkey")
            assert legacy is not None
            legacy.payment_send_address = None

        async def _rotated(_session, **_kwargs):  # type: ignore[no-untyped-def]
            return queries.EffectiveSubmissionSettings(
                revision=1,
                cooldown_seconds=3600,
                payment_address=effective,
                fee_amount_rao=40_000_000,
            )

        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.effective_submission_settings",
            AsyncMock(side_effect=_rotated),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_upload_admission_for_coldkey",
            queries.get_upload_admission_for_coldkey,
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )

        response = await client.post(
            "/api/v1/upload/check",
            json={**_signed_request_body(), "reserve_submission_slot": True},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["payment_required"] is True
        assert body["admission_token"] == str(issued.token)
        assert body["payment_send_address"] == effective


class TestCheckRecoveryUsesOnlyTheCallersReservation:
    """A coldkey's reservation for one hotkey never prices another hotkey's
    payment on /upload/check, matching /upload/agent."""

    @pytest.mark.parametrize(
        ("reservation_hotkey_is_caller", "paid_rao", "expected_status"),
        [
            # Same hotkey (archive may differ): the reserved 0.04 TAO binds.
            (True, 40_000_000, 200),
            # Another hotkey's reservation at 0.04 TAO: the current 0.09 TAO
            # applies, so the old amount is refused here, not later at upload.
            (False, 40_000_000, 402),
            (False, 90_000_000, 200),
        ],
    )
    async def test_two_hotkeys_on_one_coldkey(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        reservation_hotkey_is_caller: bool,
        paid_rao: int,
        expected_status: int,
    ) -> None:
        override_get_session(app)
        override_get_chain_client(app)
        address = app.state.config.upload_payment_address
        _real_verifier_paid_at(
            app,
            paid_at=datetime.now(UTC) - timedelta(hours=1),
            paid_rao=paid_rao,
            address=address,
        )

        async def _changed_policy(  # type: ignore[no-untyped-def]
            _session, *, default_payment_address: str, **_kwargs
        ):
            return SimpleNamespace(
                revision=7,
                cooldown_seconds=3600,
                fee_amount_rao=90_000_000,
                payment_address=default_payment_address,
                quotable=True,
            )

        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.effective_submission_settings",
            AsyncMock(side_effect=_changed_policy),
        )

        class Reservation:
            miner_hotkey = (
                _make_keypair().ss58_address
                if reservation_hotkey_is_caller
                else bittensor.Keypair.create_from_uri("//Bob").ss58_address
            )
            sha256 = "f" * 64
            fee_amount_rao = 40_000_000
            payment_send_address = address
            legacy_payment_cutoff_at = None
            expires_at = datetime.now(UTC) + timedelta(hours=2)

        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_upload_admission_for_coldkey",
            AsyncMock(return_value=Reservation()),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_evaluation_payment_for_proof",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )

        response = await client.post(
            "/api/v1/upload/check",
            json={
                **_signed_request_body(),
                "payment_block_hash": _GOOD_BLOCK_HASH,
                "payment_block_number": 13579,
                "payment_extrinsic_index": 7,
            },
        )

        assert response.status_code == expected_status, response.text
        if expected_status == 402:
            assert response.json()["error_code"] == ERROR_CODE_PAYMENT_AMOUNT_MISMATCH


class TestCheckValidatesBeforePricingRefusal:
    """Mirrors the Go relay: an unquotable revision refuses a new quote, but
    never hides a validation failure behind a pricing 503."""

    async def test_bad_signature_is_reported_not_503(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        override_get_session(app)
        override_get_chain_client(app)
        _override_payment_verifier(app)
        _unquotable_policy(monkeypatch)
        body = _signed_request_body()
        body["signature"] = _BAD_SIG

        response = await client.post("/api/v1/upload/check", json=body)

        assert response.status_code == 200, response.text
        assert ERROR_CODE_BAD_SIGNATURE in response.json()["error_codes"]

    async def test_new_reservation_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ditto.api_server.pricing.errors import UnsupportedFeeDenominationError

        override_get_session(app)
        override_get_chain_client(app)
        _override_payment_verifier(app)
        _unquotable_policy(monkeypatch)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.reserve_upload_admission",
            AsyncMock(side_effect=UnsupportedFeeDenominationError("usd_indexed")),
        )

        response = await client.post(
            "/api/v1/upload/check",
            json={**_signed_request_body(), "reserve_submission_slot": True},
        )

        assert response.status_code == 503, response.text
        assert response.json()["error_code"] == 3100


class TestNoFeeBranchesUnderUnquotablePricing:
    """Only charging the current fee is gated by quotability: credit
    redemption and archive validation are not, a fresh payment still is."""

    async def test_recorded_credit_is_redeemed(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        deps = _wire_full_stack(app)
        _unquotable_policy(monkeypatch)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        credit = SimpleNamespace(
            agent_id=None,
            credit_for_agent_id=uuid4(),
            miner_hotkey=kp.ss58_address,
            miner_coldkey="5Coldkey",
            timestamp=datetime.now(UTC),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_evaluation_payment_for_proof",
            AsyncMock(side_effect=[credit, credit]),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )
        data, files = _upload_agent_form(keypair=kp, sha256=_GOOD_TAR_SHA)

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 200, response.text
        assert response.json()["payment_disposition"] == "credit_consumed"
        deps["verifier"].verify_payment.assert_not_awaited()

    async def test_fresh_payment_without_reservation_fails_closed(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        deps = _wire_full_stack(app)
        _unquotable_policy(monkeypatch)
        kp = bittensor.Keypair.create_from_uri("//Alice")
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )
        data, files = _upload_agent_form(keypair=kp, sha256=_GOOD_TAR_SHA)

        response = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert response.status_code == 503, response.text
        assert response.json()["error_code"] == 3100
        deps["verifier"].verify_payment.assert_not_awaited()
        deps["storage"].put_object.assert_not_awaited()

    async def test_already_recorded_payment_on_check_is_reported(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        override_get_session(app)
        override_get_chain_client(app)
        verifier = _override_payment_verifier(app)
        _unquotable_policy(monkeypatch)
        credit = SimpleNamespace(
            agent_id=None,
            credit_for_agent_id=uuid4(),
            miner_hotkey=_make_keypair().ss58_address,
            miner_coldkey="5Coldkey",
            timestamp=datetime.now(UTC),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_evaluation_payment_for_proof",
            AsyncMock(return_value=credit),
        )

        response = await client.post(
            "/api/v1/upload/check",
            json={
                **_signed_request_body(),
                "payment_block_hash": _GOOD_BLOCK_HASH,
                "payment_block_number": 13579,
                "payment_extrinsic_index": 7,
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["payment_required"] is False
        verifier.verify_payment.assert_not_awaited()


def _real_db(app: FastAPI, session_maker: async_sessionmaker[AsyncSession]) -> None:
    from ditto.api_server.dependencies import get_session

    async def _session():  # type: ignore[no-untyped-def]
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _issue_reservation(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    sha256: str,
    now: datetime,
    address: str,
    fee_amount_rao: int = 40_000_000,
) -> Any:
    from ditto.db.queries import submission_settings as queries

    async with session_maker() as session, session.begin():
        return await queries.reserve_upload_admission(
            session,
            miner_coldkey="5Coldkey",
            miner_hotkey=_make_keypair().ss58_address,
            sha256=sha256,
            settings=queries.EffectiveSubmissionSettings(
                revision=1,
                cooldown_seconds=3600,
                payment_address=address,
                fee_amount_rao=fee_amount_rao,
            ),
            now=now,
        )


async def _reservation_row(
    session_maker: async_sessionmaker[AsyncSession],
) -> Any:
    from ditto.db.models import UploadAdmissionReservation

    async with session_maker() as session:
        return await session.get(UploadAdmissionReservation, "5Coldkey")


def _raised_policy(monkeypatch: pytest.MonkeyPatch, *, fee_amount_rao: int) -> None:
    from ditto.db.queries import submission_settings as queries

    async def _settings(  # type: ignore[no-untyped-def]
        _session, *, default_payment_address: str, **_kwargs
    ):
        return queries.EffectiveSubmissionSettings(
            revision=2,
            cooldown_seconds=3600,
            payment_address=default_payment_address,
            fee_amount_rao=fee_amount_rao,
        )

    monkeypatch.setattr(
        "ditto.api_server.endpoints.upload.effective_submission_settings",
        AsyncMock(side_effect=_settings),
    )


def _recovery_check_body(**overrides: Any) -> dict[str, Any]:
    return {
        **_signed_request_body(**overrides),
        "reserve_submission_slot": True,
        "payment_block_hash": _GOOD_BLOCK_HASH,
        "payment_block_number": 13579,
        "payment_extrinsic_index": 7,
    }


class TestRotationNeverExtendsAReservation:
    async def test_rotated_quote_cannot_be_chained_past_its_expiry(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Recovering a credit onto another archive rotates the token but keeps
        the original expiry, so a fresh payment made after it pays the
        current fee instead of the old reserved one."""
        _real_db(app, session_maker)
        override_get_chain_client(app)
        override_get_storage_client(app)
        address = app.state.config.upload_payment_address
        t0 = datetime.now(UTC) - timedelta(hours=23, minutes=30)
        issued = await _issue_reservation(
            session_maker, sha256="f" * 64, now=t0, address=address
        )
        _raised_policy(monkeypatch, fee_amount_rao=90_000_000)
        payment_lookup = AsyncMock(
            return_value=SimpleNamespace(
                agent_id=None,
                miner_hotkey=_make_keypair().ss58_address,
                miner_coldkey="5Coldkey",
                timestamp=t0 + timedelta(hours=1),
            )
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_evaluation_payment_for_proof",
            payment_lookup,
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )
        _override_payment_verifier(app)

        # T0+23.5h: recover the credit onto archive B (live reservation).
        check = await client.post(
            "/api/v1/upload/check",
            json=_recovery_check_body(sha256=_GOOD_TAR_SHA),
        )

        assert check.status_code == 200, check.text
        assert check.json()["payment_required"] is False
        rotated = await _reservation_row(session_maker)
        assert rotated is not None
        assert rotated.token != issued.token
        assert rotated.sha256 == _GOOD_TAR_SHA
        assert rotated.expires_at == issued.expires_at
        assert rotated.created_at == t0

        # A fresh payment at the old fee, finalized after the original expiry
        # (T0+24.5h), is held to the current fee even with the rotated token.
        payment_lookup.return_value = None
        _real_verifier_paid_at(
            app,
            paid_at=t0 + timedelta(hours=24, minutes=30),
            paid_rao=40_000_000,
            address=address,
        )
        data, files = _upload_agent_form(
            keypair=_make_keypair(),
            payment_block_hash="0x" + "c" * 64,
            payment_extrinsic_index=3,
        )
        data["admission_token"] = check.json()["admission_token"]

        upload = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert upload.status_code == 402, upload.text

    async def test_rotation_keeps_created_and_expiry_in_the_query(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        from ditto.db.queries import submission_settings as queries

        t0 = datetime.now(UTC) - timedelta(hours=2)
        issued = await _issue_reservation(
            session_maker, sha256="f" * 64, now=t0, address="5Address"
        )
        async with session_maker() as session, session.begin():
            rotated = await queries.reserve_upload_admission(
                session,
                miner_coldkey="5Coldkey",
                miner_hotkey=_make_keypair().ss58_address,
                sha256="e" * 64,
                settings=queries.EffectiveSubmissionSettings(
                    revision=2,
                    cooldown_seconds=3600,
                    payment_address="5Address",
                    fee_amount_rao=90_000_000,
                ),
                replace_existing=True,
                paid_at=t0 + timedelta(minutes=5),
            )

        assert rotated.token != issued.token
        assert rotated.expires_at == issued.expires_at
        assert rotated.fee_amount_rao == 40_000_000
        row = await _reservation_row(session_maker)
        assert row.created_at == t0


class TestConsumedProofRecovery:
    """/upload/check with a proof that already funded this exact upload."""

    def _already_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent_id = uuid4()
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_evaluation_payment_for_proof",
            AsyncMock(
                return_value=SimpleNamespace(
                    agent_id=agent_id,
                    miner_hotkey=_make_keypair().ss58_address,
                    miner_coldkey="5Coldkey",
                    timestamp=datetime.now(UTC) - timedelta(minutes=10),
                )
            ),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_agent_for_payment_proof",
            AsyncMock(
                return_value=SimpleNamespace(
                    agent_id=agent_id,
                    miner_hotkey=_make_keypair().ss58_address,
                    sha256=_GOOD_TAR_SHA,
                    name="alpha-agent",
                    version=1,
                    status="uploaded",
                )
            ),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )

    async def test_cooldown_still_applies(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _real_db(app, session_maker)
        override_get_chain_client(app)
        verifier = _override_payment_verifier(app)
        self._already_used(monkeypatch)
        retry_at = datetime.now(UTC) + timedelta(minutes=50)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_submission_retry_at",
            AsyncMock(return_value=retry_at),
        )

        response = await client.post(
            "/api/v1/upload/check", json=_recovery_check_body(sha256=_GOOD_TAR_SHA)
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is False
        assert body["admission_token"] is None
        reported = datetime.fromisoformat(body["retry_at"].replace("Z", "+00:00"))
        assert reported == retry_at
        verifier.verify_payment.assert_not_awaited()
        assert await _reservation_row(session_maker) is None

    async def test_never_takes_over_another_live_reservation(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _real_db(app, session_maker)
        override_get_chain_client(app)
        _override_payment_verifier(app)
        self._already_used(monkeypatch)
        other = await _issue_reservation(
            session_maker,
            sha256="b" * 64,
            now=datetime.now(UTC) - timedelta(minutes=1),
            address=app.state.config.upload_payment_address,
        )

        response = await client.post(
            "/api/v1/upload/check", json=_recovery_check_body(sha256=_GOOD_TAR_SHA)
        )

        assert response.status_code == 200, response.text
        assert response.json()["ok"] is False
        assert response.json()["admission_token"] is None
        kept = await _reservation_row(session_maker)
        assert kept is not None
        assert kept.token == other.token
        assert kept.sha256 == "b" * 64
        assert kept.expires_at == other.expires_at

    async def test_exact_retry_releases_the_slot_it_reserved(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _real_db(app, session_maker)
        override_get_chain_client(app)
        override_get_storage_client(app)
        verifier = _override_payment_verifier(app)
        self._already_used(monkeypatch)

        check = await client.post(
            "/api/v1/upload/check", json=_recovery_check_body(sha256=_GOOD_TAR_SHA)
        )

        assert check.status_code == 200, check.text
        assert check.json()["ok"] is True
        assert check.json()["payment_required"] is False
        assert check.json()["payment_amount_rao"] is None
        reserved = await _reservation_row(session_maker)
        assert reserved is not None
        assert str(reserved.token) == check.json()["admission_token"]

        data, files = _upload_agent_form(keypair=_make_keypair())
        data["admission_token"] = check.json()["admission_token"]
        upload = await client.post("/api/v1/upload/agent", data=data, files=files)

        assert upload.status_code == 200, upload.text
        assert upload.json()["version"] == 1
        verifier.verify_payment.assert_not_awaited()
        assert await _reservation_row(session_maker) is None


class TestCheckCreditOwnership:
    async def test_credit_from_another_coldkey_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        override_get_session(app)
        override_get_chain_client(app)
        _override_payment_verifier(app)
        reserve = AsyncMock()
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.reserve_upload_admission", reserve
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_evaluation_payment_for_proof",
            AsyncMock(
                return_value=SimpleNamespace(
                    agent_id=None,
                    miner_hotkey=_make_keypair().ss58_address,
                    miner_coldkey="5SomeoneElsesColdkey",
                    timestamp=datetime.now(UTC) - timedelta(minutes=5),
                )
            ),
        )

        response = await client.post(
            "/api/v1/upload/check", json=_recovery_check_body()
        )

        assert response.status_code == 402, response.text
        reserve.assert_not_awaited()


class TestSettledCreditUnderUnquotablePricing:
    async def test_refusal_rolls_back_and_keeps_the_reservation(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A credit paid after its reservation expired cannot be given a new
        quote from an unquotable revision. The refusal is fail-closed (503)
        and transactional: the old reservation is not deleted and the credit
        is untouched, so recovery succeeds once pricing is quotable again."""
        _real_db(app, session_maker)
        override_get_chain_client(app)
        _override_payment_verifier(app)
        quoted_at = datetime.now(UTC) - timedelta(hours=25)
        issued = await _issue_reservation(
            session_maker,
            sha256=_GOOD_SHA256,
            now=quoted_at,
            address=app.state.config.upload_payment_address,
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_evaluation_payment_for_proof",
            AsyncMock(
                return_value=SimpleNamespace(
                    agent_id=None,
                    miner_hotkey=_make_keypair().ss58_address,
                    miner_coldkey="5Coldkey",
                    timestamp=quoted_at + timedelta(hours=24, minutes=30),
                )
            ),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.upload.get_same_owner_agent_by_sha",
            AsyncMock(return_value=None),
        )
        _unquotable_policy(monkeypatch)

        response = await client.post(
            "/api/v1/upload/check", json=_recovery_check_body()
        )

        assert response.status_code == 503, response.text
        assert response.json()["error_code"] == 3100
        kept = await _reservation_row(session_maker)
        assert kept is not None
        assert kept.token == issued.token
        assert kept.expires_at == issued.expires_at
