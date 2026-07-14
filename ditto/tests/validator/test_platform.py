"""Validator platform client authentication tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import bittensor
import httpx

from ditto.api_models.validator import JobRequest
from ditto.validator.platform import PlatformClient
from ditto.validator.signing import job_signing_message


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
