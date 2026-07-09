"""Async client for the platform's ``/validator/*`` HTTP API.

The worker is HTTP-decoupled from the platform: it pulls work and writes scores
over the same public contract any external validator would use, authenticating
with the ``X-Validator-Hotkey`` header and (on score submit) an sr25519
signature. It never touches the platform DB directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

import httpx

from ditto.api_models.validator import (
    ArtifactResponse,
    JobResponse,
    LedgerResponse,
    ScoreReport,
    SubmitScoreRequest,
    SubmitScoreResponse,
    ValidatorQueueResponse,
)
from ditto.validator.errors import PlatformError

if TYPE_CHECKING:
    from ditto.validator.config import ValidatorConfig

logger = logging.getLogger(__name__)

_PREFIX = "/api/v1/validator"
# The scoring ledger lives under a sibling prefix, not /validator.
_SCORING_PREFIX = "/api/v1/scoring"


class PlatformClient:
    """HTTP client for one platform base URL."""

    def __init__(self, config: ValidatorConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client
        self._base = config.platform_api_url.rstrip("/")
        self._headers = {"X-Validator-Hotkey": config.validator_hotkey}

    async def get_queue(self) -> ValidatorQueueResponse:
        """Pull agents awaiting evaluation."""
        url = f"{self._base}{_PREFIX}/queue"
        params = {"limit": self._config.queue_limit}
        try:
            resp = await self._client.get(url, params=params, headers=self._headers)
        except httpx.HTTPError as e:
            raise PlatformError(f"queue fetch failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"queue rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return ValidatorQueueResponse.model_validate(resp.json())

    async def request_job(self) -> JobResponse | None:
        """Request a scoring ticket (the k=3 pull). ``None`` on 204 (no work).

        POST /validator/job issues at most :data:`SCORING_QUORUM` tickets per
        agent to distinct validators, so most calls return 204. A returned ticket
        carries the pinned dataset (``seed`` + ``dataset_sha256`` + ``run_size``)
        and the ``deadline`` to score by.
        """
        url = f"{self._base}{_PREFIX}/job"
        try:
            resp = await self._client.post(url, headers=self._headers)
        except httpx.HTTPError as e:
            raise PlatformError(f"job request failed: {e}") from e
        if resp.status_code == 204:
            return None
        if resp.status_code != 200:
            raise PlatformError(
                f"job request rejected ({resp.status_code}): {resp.text[:200]}"
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
        try:
            resp = await self._client.get(url, headers=self._headers)
        except httpx.HTTPError as e:
            raise PlatformError(f"ledger fetch failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"ledger rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return LedgerResponse.model_validate(resp.json())

    async def get_artifact(self, agent_id: UUID) -> ArtifactResponse:
        """Get a presigned tarball download URL for ``agent_id``."""
        url = f"{self._base}{_PREFIX}/agent/{agent_id}/artifact"
        try:
            resp = await self._client.get(url, headers=self._headers)
        except httpx.HTTPError as e:
            raise PlatformError(f"artifact fetch failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"artifact rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return ArtifactResponse.model_validate(resp.json())

    async def submit_score(
        self, agent_id: UUID, *, signature: str, report: ScoreReport
    ) -> SubmitScoreResponse:
        """Report a signed score for ``agent_id``."""
        url = f"{self._base}{_PREFIX}/agent/{agent_id}/score"
        payload = SubmitScoreRequest(
            validator_hotkey=self._config.validator_hotkey,
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
