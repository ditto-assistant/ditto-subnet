"""Async client for the platform's ``/validator/*`` HTTP API.

The worker is HTTP-decoupled from the platform: it pulls work and writes scores
over the same public contract any external validator would use, authenticating
with the ``X-Validator-Hotkey`` header and (on score submit) an sr25519
signature. It never touches the platform DB directly.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import httpx

from ditto.api_models.validator import (
    ArtifactResponse,
    ConfirmationJobRequest,
    JobRequest,
    JobResponse,
    LedgerResponse,
    ScoreReport,
    SubmitScoreRequest,
    SubmitScoreResponse,
    ValidatorHeartbeatRequest,
    ValidatorHeartbeatResponse,
)
from ditto.validator.errors import PlatformError
from ditto.validator.signing import (
    sign_artifact_request,
    sign_confirmation_job_request,
    sign_job_request,
    sign_ledger_request,
)

if TYPE_CHECKING:
    from ditto.validator.config import ValidatorConfig

logger = logging.getLogger(__name__)

_PREFIX = "/api/v1/validator"
# The scoring ledger lives under a sibling prefix, not /validator.
_SCORING_PREFIX = "/api/v1/scoring"


class PlatformClient:
    """HTTP client for one platform base URL."""

    def __init__(
        self, config: ValidatorConfig, client: httpx.AsyncClient, keypair: Any
    ) -> None:
        self._config = config
        self._client = client
        self._keypair = keypair
        self._base = config.platform_api_url.rstrip("/")
        self._headers = {"X-Validator-Hotkey": config.validator_hotkey}

    async def submit_heartbeat(
        self, request: ValidatorHeartbeatRequest
    ) -> ValidatorHeartbeatResponse:
        """Publish this hotkey's signed software identity."""
        url = f"{self._base}{_PREFIX}/heartbeat"
        try:
            resp = await self._client.post(
                url, json=request.model_dump(mode="json"), headers=self._headers
            )
        except httpx.HTTPError as e:
            raise PlatformError(f"heartbeat failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"heartbeat rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return ValidatorHeartbeatResponse.model_validate(resp.json())

    async def request_job(self) -> JobResponse | None:
        """Request a scoring ticket (the k=3 pull). ``None`` on 204 (no work).

        POST /validator/job issues at most :data:`SCORING_QUORUM` tickets per
        agent to distinct validators, so most calls return 204. A returned ticket
        carries the pinned dataset (``seed`` + ``dataset_sha256`` + ``run_size``)
        and the ``deadline`` to score by.
        """
        url = f"{self._base}{_PREFIX}/job"
        requested_at = datetime.now(UTC)
        nonce = uuid4()
        payload = JobRequest(
            validator_hotkey=self._config.validator_hotkey,
            nonce=nonce,
            requested_at=requested_at,
            signature=sign_job_request(
                self._keypair,
                validator_hotkey=self._config.validator_hotkey,
                nonce=nonce,
                requested_at=requested_at,
            ),
        )
        try:
            resp = await self._client.post(
                url, headers=self._headers, json=payload.model_dump(mode="json")
            )
        except httpx.HTTPError as e:
            raise PlatformError(f"job request failed: {e}") from e
        if resp.status_code == 204:
            return None
        if resp.status_code != 200:
            raise PlatformError(
                f"job request rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return JobResponse.model_validate(resp.json())

    async def request_confirmation_job(
        self, *, champion_agent_id: UUID, challenger_agent_id: UUID
    ) -> JobResponse:
        """Claim the one platform-validated uncertainty-band confirmation."""
        url = f"{self._base}{_PREFIX}/confirmation-job"
        requested_at = datetime.now(UTC)
        nonce = uuid4()
        payload = ConfirmationJobRequest(
            validator_hotkey=self._config.validator_hotkey,
            champion_agent_id=champion_agent_id,
            challenger_agent_id=challenger_agent_id,
            nonce=nonce,
            requested_at=requested_at,
            signature=sign_confirmation_job_request(
                self._keypair,
                validator_hotkey=self._config.validator_hotkey,
                champion_agent_id=champion_agent_id,
                challenger_agent_id=challenger_agent_id,
                nonce=nonce,
                requested_at=requested_at,
            ),
        )
        try:
            resp = await self._client.post(
                url, headers=self._headers, json=payload.model_dump(mode="json")
            )
        except httpx.HTTPError as e:
            raise PlatformError(f"confirmation job request failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"confirmation job rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return JobResponse.model_validate(resp.json())

    async def get_ledger(self) -> LedgerResponse:
        """Pull the best-score-per-miner ledger the worker folds into weights.

        This is the durable scoring pool (``GET /scoring/scores``) — the source
        of the on-chain weight vector every epoch, so a scored agent keeps its
        weight until genuinely dethroned instead of being zeroed the moment it
        leaves the ``evaluating`` queue.
        """
        url = f"{self._base}{_SCORING_PREFIX}/scores"
        requested_at = datetime.now(UTC)
        nonce = uuid4()
        proof_headers = {
            **self._headers,
            "X-Validator-Ledger-Nonce": str(nonce),
            "X-Validator-Ledger-Requested-At": requested_at.isoformat(),
            "X-Validator-Ledger-Signature": sign_ledger_request(
                self._keypair,
                validator_hotkey=self._config.validator_hotkey,
                nonce=nonce,
                requested_at=requested_at,
            ),
        }
        try:
            resp = await self._client.get(url, headers=proof_headers)
        except httpx.HTTPError as e:
            raise PlatformError(f"ledger fetch failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"ledger rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return LedgerResponse.model_validate(resp.json())

    async def get_artifact(self, agent_id: UUID) -> ArtifactResponse:
        """Get a presigned tarball URL with fresh proof of hotkey ownership."""
        url = f"{self._base}{_PREFIX}/agent/{agent_id}/artifact"
        requested_at = datetime.now(UTC)
        nonce = uuid4()
        proof_headers = {
            **self._headers,
            "X-Validator-Artifact-Nonce": str(nonce),
            "X-Validator-Artifact-Requested-At": requested_at.isoformat(),
            "X-Validator-Artifact-Signature": sign_artifact_request(
                self._keypair,
                validator_hotkey=self._config.validator_hotkey,
                agent_id=agent_id,
                nonce=nonce,
                requested_at=requested_at,
            ),
        }
        try:
            resp = await self._client.get(url, headers=proof_headers)
        except httpx.HTTPError as e:
            raise PlatformError(f"artifact fetch failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"artifact rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return ArtifactResponse.model_validate(resp.json())

    async def submit_score(
        self,
        agent_id: UUID,
        *,
        signature: str,
        report: ScoreReport,
        ticket_deadline: datetime | None = None,
    ) -> SubmitScoreResponse:
        """Report a signed score for ``agent_id``."""
        url = f"{self._base}{_PREFIX}/agent/{agent_id}/score"
        payload = SubmitScoreRequest(
            validator_hotkey=self._config.validator_hotkey,
            ticket_deadline=ticket_deadline,
            signature=signature,
            report=report,
        )
        try:
            resp = await self._client.post(
                url, json=payload.model_dump(mode="json"), headers=self._headers
            )
        except httpx.HTTPError as e:
            raise PlatformError(f"score submit failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"score rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return SubmitScoreResponse.model_validate(resp.json())
