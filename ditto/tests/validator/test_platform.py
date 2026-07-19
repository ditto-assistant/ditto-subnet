"""Validator platform client authentication tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import bittensor
import httpx
import pytest

from ditto.api_models.validator import (
    ConfirmationJobRequest,
    JobRequest,
    ValidatorHeartbeatRequest,
)
from ditto.validator.errors import PlatformError
from ditto.validator.platform import PlatformClient
from ditto.validator.signing import (
    artifact_signing_message,
    confirmation_job_signing_message,
    job_signing_message,
    ledger_signing_message,
)


async def test_job_claim_is_fresh_and_signed_by_validator_hotkey() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Validator-Hotkey"] == keypair.ss58_address
        claim = JobRequest.model_validate(json.loads(request.content))
        message = job_signing_message(
            validator_hotkey=claim.validator_hotkey,
            nonce=claim.nonce,
            requested_at=claim.requested_at,
        )
        assert keypair.verify(message, bytes.fromhex(claim.signature))
        return httpx.Response(204)

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        platform = PlatformClient(config, http, keypair)  # type: ignore[arg-type]
        assert await platform.request_job() is None


async def test_confirmation_claim_binds_champion_and_challenger() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    champion = UUID("550e8400-e29b-41d4-a716-446655440000")
    challenger = UUID("c9bf9e57-1685-4c89-bafb-ff5af830be8a")

    def handler(request: httpx.Request) -> httpx.Response:
        claim = ConfirmationJobRequest.model_validate(json.loads(request.content))
        message = confirmation_job_signing_message(
            validator_hotkey=claim.validator_hotkey,
            champion_agent_id=claim.champion_agent_id,
            challenger_agent_id=claim.challenger_agent_id,
            nonce=claim.nonce,
            requested_at=claim.requested_at,
        )
        assert keypair.verify(message, bytes.fromhex(claim.signature))
        return httpx.Response(
            200,
            json={
                "agent_id": str(challenger),
                "miner_hotkey": "5Miner",
                "sha256": "ab" * 32,
                "deadline": datetime.now(UTC).isoformat(),
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        job = await PlatformClient(config, http, keypair).request_confirmation_job(  # type: ignore[arg-type]
            champion_agent_id=champion,
            challenger_agent_id=challenger,
        )
    assert job.agent_id == challenger


async def test_artifact_request_is_fresh_agent_bound_and_signed() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")

    def handler(request: httpx.Request) -> httpx.Response:
        nonce = UUID(request.headers["X-Validator-Artifact-Nonce"])
        requested_at = datetime.fromisoformat(
            request.headers["X-Validator-Artifact-Requested-At"]
        )
        message = artifact_signing_message(
            validator_hotkey=keypair.ss58_address,
            agent_id=agent_id,
            nonce=nonce,
            requested_at=requested_at,
        )
        assert keypair.verify(
            message,
            bytes.fromhex(request.headers["X-Validator-Artifact-Signature"]),
        )
        return httpx.Response(
            200,
            json={
                "agent_id": str(agent_id),
                "sha256": "ab" * 32,
                "download_url": "https://storage.test/artifact",
                "expires_at": datetime.now(UTC).isoformat(),
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).get_artifact(agent_id)

    assert response.agent_id == agent_id


async def test_ledger_request_is_fresh_and_signed() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")

    def handler(request: httpx.Request) -> httpx.Response:
        nonce = UUID(request.headers["X-Validator-Ledger-Nonce"])
        requested_at = datetime.fromisoformat(
            request.headers["X-Validator-Ledger-Requested-At"]
        )
        message = ledger_signing_message(
            validator_hotkey=keypair.ss58_address,
            nonce=nonce,
            requested_at=requested_at,
        )
        assert keypair.verify(
            message,
            bytes.fromhex(request.headers["X-Validator-Ledger-Signature"]),
        )
        return httpx.Response(
            200,
            json={
                "entries": [],
                "count": 0,
                "generated_at": datetime.now(UTC).isoformat(),
                "stale": False,
                "age_seconds": 0,
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).get_ledger()

    assert response.entries == []


_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def _config() -> MagicMock:
    config = MagicMock()
    config.platform_api_url = "https://platform.example"
    config.validator_hotkey = _HOTKEY
    return config


def _request() -> ValidatorHeartbeatRequest:
    return ValidatorHeartbeatRequest(
        validator_hotkey=_HOTKEY,
        software_version="0.1.0",
        protocol_version=1,
        code_digest="ab" * 32,
        state="running_benchmark",
        timestamp=1_752_443_200,
        signature="cd" * 64,
    )


async def test_submit_heartbeat_posts_signed_contract() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["header"] = request.headers["X-Validator-Hotkey"]
        captured["body"] = request.read()
        return httpx.Response(
            200,
            json={"accepted": True, "seen_at": datetime.now(UTC).isoformat()},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await PlatformClient(_config(), http, MagicMock()).submit_heartbeat(
            _request()
        )

    assert response.accepted is True
    assert captured["url"] == "https://platform.example/api/v1/validator/heartbeat"
    assert captured["header"] == _HOTKEY
    assert b'"software_version":"0.1.0"' in captured["body"]


async def test_submit_heartbeat_surfaces_rejection() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="no")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformError, match=r"heartbeat rejected \(401\)"):
            await PlatformClient(_config(), http, MagicMock()).submit_heartbeat(
                _request()
            )
