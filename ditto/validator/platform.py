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
from pydantic import ValidationError

from ditto.api_models.inference import (
    InferenceExchangeRequest,
    InferenceExchangeResponse,
)
from ditto.api_models.validator import (
    ArtifactResponse,
    FailJobReason,
    FailJobRequest,
    FailJobResponse,
    JobRequest,
    JobResponse,
    LedgerResponse,
    ScoreReport,
    SubmitScoreRequest,
    SubmitScoreResponse,
    Top5ConfirmationJobRequest,
    ValidatorHeartbeatRequest,
    ValidatorHeartbeatResponse,
)
from ditto.validator.errors import PlatformError
from ditto.validator.signing import (
    sign_artifact_request,
    sign_inference_exchange,
    sign_job_fail_request,
    sign_job_request,
    sign_ledger_request,
    sign_top5_confirmation_job_request,
    sign_top5_confirmation_score,
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

    async def request_job(self, slot_id: str | None = None) -> JobResponse | None:
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
            slot_id=slot_id,
            nonce=nonce,
            requested_at=requested_at,
            signature=sign_job_request(
                self._keypair,
                validator_hotkey=self._config.validator_hotkey,
                nonce=nonce,
                requested_at=requested_at,
                slot_id=slot_id,
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

    async def exchange_inference_grant(
        self, grant_id: UUID, broker_public_key: str, exchange_url: str
    ) -> InferenceExchangeResponse:
        """Authorize one trusted broker key for the exact live ticket grant."""
        expected_url = f"{self._base}/api/v1/inference/exchange"
        if exchange_url.rstrip("/") != expected_url:
            raise PlatformError("ticket inference exchange URL is not the platform")
        requested_at = datetime.now(UTC)
        nonce = uuid4()
        payload = InferenceExchangeRequest(
            validator_hotkey=self._config.validator_hotkey,
            grant_id=grant_id,
            broker_public_key=broker_public_key,
            nonce=nonce,
            requested_at=requested_at,
            signature=sign_inference_exchange(
                self._keypair,
                validator_hotkey=self._config.validator_hotkey,
                grant_id=grant_id,
                broker_public_key=broker_public_key,
                nonce=nonce,
                requested_at=requested_at,
            ),
        )
        try:
            response = await self._client.post(
                expected_url,
                headers=self._headers,
                json=payload.model_dump(mode="json"),
            )
        except httpx.HTTPError as error:
            raise PlatformError(f"inference exchange failed: {error}") from error
        if response.status_code != 200:
            raise PlatformError(
                f"inference exchange rejected ({response.status_code}): "
                f"{response.text[:200]}"
            )
        return InferenceExchangeResponse.model_validate(response.json())

    async def report_ticket_failed(
        self, job: JobResponse, reason: FailJobReason
    ) -> FailJobResponse:
        """Hand a failed ticket back so the platform reissues a fresh lease.

        POST /validator/job/fail closes the still-live lease for
        ``(job.agent_id, job.deadline)`` immediately (rather than waiting for it
        to expire) so the next :meth:`request_job` mints a brand-new ticket
        instead of resuming the failed attempt. Raises :class:`PlatformError` on
        any non-200; callers MUST treat this as best-effort and never let a
        failed report crash the scoring sweep — an old platform without this
        endpoint just leaves the ticket to expire on its own, exactly as before.
        """
        url = f"{self._base}{_PREFIX}/job/fail"
        requested_at = datetime.now(UTC)
        nonce = uuid4()
        payload = FailJobRequest(
            validator_hotkey=self._config.validator_hotkey,
            agent_id=job.agent_id,
            ticket_deadline=job.deadline,
            reason=reason,
            nonce=nonce,
            requested_at=requested_at,
            signature=sign_job_fail_request(
                self._keypair,
                validator_hotkey=self._config.validator_hotkey,
                agent_id=job.agent_id,
                ticket_deadline=job.deadline,
                nonce=nonce,
                requested_at=requested_at,
            ),
        )
        try:
            resp = await self._client.post(
                url, headers=self._headers, json=payload.model_dump(mode="json")
            )
        except httpx.HTTPError as e:
            raise PlatformError(f"job fail report failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"job fail report rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return FailJobResponse.model_validate(resp.json())

    async def request_top5_confirmation_job(
        self, *, champion_agent_id: UUID, member_agent_id: UUID
    ) -> JobResponse:
        """Claim a lease for one member of the top-5 shared-seed rescore lane.

        Generalizes :meth:`request_confirmation_job` from the single uncertain
        raw leader to any emission-set member (champion or tail). The platform
        rebuilds the same current-version KOTH projection, verifies the claimed
        ``champion_agent_id`` is the reigning incumbent and ``member_agent_id``
        is either that champion or a current tail entrant, gates issuance on the
        rescore tempo, and grants a confirmation ticket the ordinary
        ``submit_score`` path can bind to (the #195 mechanism, generalized). The
        validator then benchmarks ``member_agent_id`` on the champion-anchored
        CRN seeds and submits through the ordinary score API with the granted
        deadline. This is the single point of contact with the platform's top-5
        lane; the wire contract is reconciled against the parallel ditto-platform
        PR (see ``docs/top5-rescore-lane.md``).
        """
        url = f"{self._base}{_PREFIX}/top5-confirmation-job"
        requested_at = datetime.now(UTC)
        nonce = uuid4()
        payload = Top5ConfirmationJobRequest(
            validator_hotkey=self._config.validator_hotkey,
            champion_agent_id=champion_agent_id,
            member_agent_id=member_agent_id,
            nonce=nonce,
            requested_at=requested_at,
            signature=sign_top5_confirmation_job_request(
                self._keypair,
                validator_hotkey=self._config.validator_hotkey,
                champion_agent_id=champion_agent_id,
                member_agent_id=member_agent_id,
                nonce=nonce,
                requested_at=requested_at,
            ),
        )
        try:
            resp = await self._client.post(
                url, headers=self._headers, json=payload.model_dump(mode="json")
            )
        except httpx.HTTPError as e:
            raise PlatformError(f"top-5 confirmation job request failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"top-5 confirmation job rejected "
                f"({resp.status_code}): {resp.text[:200]}"
            )
        return JobResponse.model_validate(resp.json())

    async def get_ledger(self) -> LedgerResponse:
        """Pull the best-score-per-payment-coldkey ledger folded into weights.

        The platform resolves ownership from the immutable coldkey captured at
        payment time and returns only the winning generation's hotkey. The
        validator must weight that returned hotkey without re-resolving current
        chain ownership.

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
        try:
            return ArtifactResponse.model_validate(resp.json())
        except (ValidationError, ValueError) as e:
            # A malformed artifact is scoped to one ticket. Normalize model/JSON
            # failures to the worker's typed platform boundary so the ticket is
            # skipped without abandoning the remainder of the scoring sweep.
            raise PlatformError("artifact response was invalid") from e

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

    async def submit_top5_confirmation_score(
        self,
        agent_id: UUID,
        *,
        report: ScoreReport,
        ticket_deadline: datetime,
    ) -> SubmitScoreResponse:
        """Append shared-seed evidence without replacing the canonical score."""
        if (
            report.bench_version is None
            or report.confirmation_seeds is None
            or report.confirmation_composites is None
        ):
            raise PlatformError("top-5 confirmation report is incomplete")
        signature = sign_top5_confirmation_score(
            self._keypair,
            validator_hotkey=self._config.validator_hotkey,
            agent_id=agent_id,
            ticket_deadline=ticket_deadline,
            run_id=report.run_id,
            bench_version=report.bench_version,
            confirmation_seeds=report.confirmation_seeds,
            confirmation_composites=report.confirmation_composites,
        )
        url = f"{self._base}{_PREFIX}/agent/{agent_id}/top5-confirmation-score"
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
        except httpx.HTTPError as exc:
            raise PlatformError(f"top-5 confirmation submit failed: {exc}") from exc
        if resp.status_code != 200:
            raise PlatformError(
                f"top-5 confirmation rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return SubmitScoreResponse.model_validate(resp.json())

    async def submit_transcript(
        self, agent_id: UUID, *, run_id: str, body: bytes
    ) -> None:
        """Publish the run's transcript artifact behind an already-submitted score.

        ``PUT /validator/agent/{id}/transcript/{run_id}`` with the raw canonical
        bytes. The platform accepts them only when their SHA-256 equals the
        digest the signed score declared (``details["transcript_sha256"]``) and
        stores them content-addressed in the public bucket. Raises
        :class:`PlatformError` on rejection so the caller can log it; callers
        treat failure as best-effort (the score already stands)."""
        url = f"{self._base}{_PREFIX}/agent/{agent_id}/transcript/{run_id}"
        try:
            resp = await self._client.put(url, content=body, headers=self._headers)
        except httpx.HTTPError as e:
            raise PlatformError(f"transcript submit failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"transcript rejected ({resp.status_code}): {resp.text[:200]}"
            )
