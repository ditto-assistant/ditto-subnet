"""Async client for the platform's ``/validator/*`` HTTP API.

The worker is HTTP-decoupled from the platform: it pulls work and writes scores
over the same public contract any external validator would use, authenticating
with the ``X-Validator-Hotkey`` header and (on score submit) an sr25519
signature. It never touches the platform DB directly.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError

from ditto.api_models.inference import (
    InferenceExchangeRequest,
    InferenceExchangeResponse,
)
from ditto.api_models.validator import (
    LEGACY_FAILURE_DETAIL_MAX_LENGTH,
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
from ditto.api_models.validator_confirmation import (
    V9ConfirmationClaimRequest,
    V9ConfirmationCompletionReport,
    V9ConfirmationFailRequest,
    V9ConfirmationFailResponse,
    V9ConfirmationJobResponse,
    V9ConfirmationPreparedReport,
    V9ConfirmationPrepareRequest,
    V9ConfirmationScorerResult,
    V9ConfirmationSubmitRequest,
    V9ConfirmationSubmitResponse,
)
from ditto.validator.errors import (
    PlatformError,
    PlatformInfrastructureError,
    truncate_failure_detail,
)
from ditto.validator.signing import (
    sign_artifact_request,
    sign_inference_exchange,
    sign_job_fail_request,
    sign_job_request,
    sign_ledger_request,
    sign_top5_confirmation_job_request,
    sign_top5_confirmation_score,
    sign_v9_confirmation_artifact_request,
    sign_v9_confirmation_claim,
    sign_v9_confirmation_fail,
    sign_v9_confirmation_prepare,
    v9_confirmation_prepare_wire_sha256,
    verify_ledger_entry,
)

if TYPE_CHECKING:
    from ditto.validator.config import ValidatorConfig

logger = logging.getLogger(__name__)

_PREFIX = "/api/v1/validator"
# The scoring ledger lives under a sibling prefix, not /validator.
_SCORING_PREFIX = "/api/v1/scoring"
# Exchange is idempotent for one signed nonce. Keep fast recovery for ordinary
# blips, but survive a full relay handover window without throwing away a
# benchmark ticket. This does not spend inference budget: no model request can
# begin until the exchange succeeds.
_INFERENCE_EXCHANGE_RETRY_DELAYS = (0.25, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
_INFERENCE_BUDGET_EVIDENCE_HEADERS = {
    "request_budget": "X-Ditto-Request-Budget",
    "token_budget": "X-Ditto-Token-Budget",
    "embedding_request_budget": "X-Ditto-Embedding-Request-Budget",
    "embedding_token_budget": "X-Ditto-Embedding-Token-Budget",
    "max_output_tokens": "X-Ditto-Max-Output-Tokens",
}


def _inference_budget_evidence(response: httpx.Response) -> dict[str, int]:
    """Read all-or-nothing trusted budget evidence from exchange headers."""
    raw = {
        field: response.headers.get(header)
        for field, header in _INFERENCE_BUDGET_EVIDENCE_HEADERS.items()
    }
    if all(value is None for value in raw.values()):
        return {}
    if any(value is None for value in raw.values()):
        raise PlatformInfrastructureError(
            "inference exchange returned incomplete budget evidence"
        )
    try:
        evidence = {field: int(value) for field, value in raw.items() if value}
    except ValueError as error:
        raise PlatformInfrastructureError(
            "inference exchange returned invalid budget evidence"
        ) from error
    if len(evidence) != len(raw) or any(value < 1 for value in evidence.values()):
        raise PlatformInfrastructureError(
            "inference exchange returned invalid budget evidence"
        )
    return evidence


class PlatformClient:
    """HTTP client for one platform base URL."""

    def __init__(
        self, config: ValidatorConfig, client: httpx.AsyncClient, keypair: Any
    ) -> None:
        self._config = config
        self._client = client
        self._keypair = keypair
        self._base = config.platform_api_url.rstrip("/")
        self._inference_base = (
            getattr(config, "platform_inference_base_url", "")
            or config.platform_api_url
        ).rstrip("/")
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

    async def request_v9_confirmation_job(
        self,
        *,
        slot_id: str,
        profile_revision: str,
        profile_checksum: str,
        broker_public_key: str,
    ) -> V9ConfirmationJobResponse | None:
        """Claim one internal exact-profile bundle; ``None`` means no work."""
        requested_at = datetime.now(UTC)
        nonce = uuid4()
        payload = V9ConfirmationClaimRequest(
            validator_hotkey=self._config.validator_hotkey,
            slot_id=slot_id,
            profile_revision=profile_revision,
            profile_checksum=profile_checksum,
            broker_public_key=broker_public_key,
            nonce=nonce,
            requested_at=requested_at,
            signature=sign_v9_confirmation_claim(
                self._keypair,
                validator_hotkey=self._config.validator_hotkey,
                slot_id=slot_id,
                profile_revision=profile_revision,
                profile_checksum=profile_checksum,
                broker_public_key=broker_public_key,
                nonce=nonce,
                requested_at=requested_at,
            ),
        )
        url = f"{self._base}{_PREFIX}/v9-confirmation/job"
        try:
            response = await self._client.post(
                url,
                headers=self._headers,
                json=payload.model_dump(mode="json"),
            )
        except httpx.HTTPError as error:
            raise PlatformError(f"v9 confirmation claim failed: {error}") from error
        if response.status_code == 204:
            return None
        if response.status_code != 200:
            raise PlatformError(
                "v9 confirmation claim rejected "
                f"({response.status_code}): {response.text[:200]}"
            )
        try:
            job = V9ConfirmationJobResponse.model_validate(response.json())
        except (ValidationError, ValueError) as error:
            raise PlatformError("v9 confirmation job response was invalid") from error
        if (
            job.slot_id != slot_id
            or job.execution_profile.revision != profile_revision
            or job.execution_profile.checksum != profile_checksum
        ):
            raise PlatformError(
                "v9 confirmation job response did not match the signed claim"
            )
        return job

    async def get_v9_confirmation_artifact(
        self, job: V9ConfirmationJobResponse
    ) -> ArtifactResponse:
        """Fetch source through the bundle ticket, never a canonical ticket."""
        requested_at = datetime.now(UTC)
        nonce = uuid4()
        headers = {
            **self._headers,
            "X-Confirmation-Ticket-Id": str(job.ticket_id),
            "X-Confirmation-Nonce": str(nonce),
            "X-Confirmation-Requested-At": requested_at.isoformat(),
            "X-Confirmation-Signature": sign_v9_confirmation_artifact_request(
                self._keypair,
                validator_hotkey=self._config.validator_hotkey,
                bundle_id=job.bundle_id,
                ticket_id=job.ticket_id,
                nonce=nonce,
                requested_at=requested_at,
            ),
        }
        url = f"{self._base}{_PREFIX}/v9-confirmation/bundle/{job.bundle_id}/artifact"
        try:
            response = await self._client.get(url, headers=headers)
        except httpx.HTTPError as error:
            raise PlatformError(
                f"v9 confirmation artifact fetch failed: {error}"
            ) from error
        if response.status_code != 200:
            raise PlatformError(
                "v9 confirmation artifact rejected "
                f"({response.status_code}): {response.text[:200]}"
            )
        try:
            artifact = ArtifactResponse.model_validate(response.json())
        except (ValidationError, ValueError) as error:
            raise PlatformError(
                "v9 confirmation artifact response was invalid"
            ) from error
        if (
            artifact.agent_id != job.agent_id
            or artifact.sha256 != job.artifact_sha256
            or artifact.bench_version != 9
        ):
            raise PlatformError("v9 confirmation artifact identity mismatch")
        return artifact

    async def prepare_v9_confirmation_report(
        self,
        job: V9ConfirmationJobResponse,
        result: V9ConfirmationScorerResult,
    ) -> V9ConfirmationPreparedReport:
        """Normalize native scorer evidence and derive its Platform-owned root."""
        requested_at = datetime.now(UTC)
        nonce = uuid4()
        longmemeval = result.longmemeval.model_dump(mode="json")
        inference_ablation = result.inference_ablation.model_dump(mode="json")
        embedding_ablation = result.embedding_ablation.model_dump(mode="json")
        wire_sha256 = v9_confirmation_prepare_wire_sha256(
            ablation_coordinator_latency_ms=result.ablation_coordinator_latency_ms,
            longmemeval=longmemeval,
            inference_ablation=inference_ablation,
            embedding_ablation=embedding_ablation,
        )
        if result.evidence_sha256 != wire_sha256:
            raise PlatformError("v9 confirmation scorer native wire digest was invalid")
        payload = V9ConfirmationPrepareRequest(
            validator_hotkey=self._config.validator_hotkey,
            ticket_id=job.ticket_id,
            nonce=nonce,
            requested_at=requested_at,
            wire_sha256=wire_sha256,
            ablation_coordinator_latency_ms=result.ablation_coordinator_latency_ms,
            longmemeval=result.longmemeval,
            inference_ablation=result.inference_ablation,
            embedding_ablation=result.embedding_ablation,
            signature=sign_v9_confirmation_prepare(
                self._keypair,
                validator_hotkey=self._config.validator_hotkey,
                bundle_id=job.bundle_id,
                ticket_id=job.ticket_id,
                wire_sha256=wire_sha256,
                nonce=nonce,
                requested_at=requested_at,
            ),
        )
        url = (
            f"{self._base}{_PREFIX}/v9-confirmation/"
            f"bundle/{job.bundle_id}/prepare-report"
        )
        try:
            response = await self._client.post(
                url,
                headers=self._headers,
                json=payload.model_dump(mode="json"),
            )
        except httpx.HTTPError as error:
            raise PlatformError(
                f"v9 confirmation report preparation failed: {error}"
            ) from error
        if response.status_code != 200:
            raise PlatformError(
                "v9 confirmation report preparation rejected "
                f"({response.status_code}): {response.text[:200]}"
            )
        try:
            prepared = V9ConfirmationPreparedReport.model_validate(response.json())
        except (ValidationError, ValueError) as error:
            raise PlatformError(
                "v9 confirmation prepared report response was invalid"
            ) from error
        if (
            prepared.bundle_id != job.bundle_id
            or prepared.ticket_id != job.ticket_id
            or prepared.ablation_coordinator_latency_ms
            != result.ablation_coordinator_latency_ms
        ):
            raise PlatformError("v9 confirmation prepared report identity mismatch")
        return prepared

    async def submit_v9_confirmation_report(
        self,
        job: V9ConfirmationJobResponse,
        report: V9ConfirmationCompletionReport,
    ) -> V9ConfirmationSubmitResponse:
        payload = V9ConfirmationSubmitRequest(
            validator_hotkey=self._config.validator_hotkey,
            ticket_id=job.ticket_id,
            report=report,
        )
        url = f"{self._base}{_PREFIX}/v9-confirmation/bundle/{job.bundle_id}/report"
        response: httpx.Response | None = None
        for attempt, delay in enumerate((0.0, 0.25, 1.0)):
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await self._client.post(
                    url,
                    headers=self._headers,
                    json=payload.model_dump(mode="json"),
                )
            except httpx.HTTPError as error:
                if attempt < 2:
                    continue
                raise PlatformError(
                    f"v9 confirmation report failed after 3 attempts: {error}"
                ) from error
            if response.status_code == 200:
                break
            if response.status_code not in {408, 429} and response.status_code < 500:
                break
        assert response is not None
        if response.status_code != 200:
            raise PlatformError(
                "v9 confirmation report rejected "
                f"({response.status_code}): {response.text[:200]}"
            )
        try:
            accepted = V9ConfirmationSubmitResponse.model_validate(response.json())
        except (ValidationError, ValueError) as error:
            raise PlatformError(
                "v9 confirmation submit response was invalid"
            ) from error
        if accepted.bundle_id != job.bundle_id or accepted.ticket_id != job.ticket_id:
            raise PlatformError("v9 confirmation acceptance identity mismatch")
        return accepted

    async def fail_v9_confirmation_job(
        self,
        job: V9ConfirmationJobResponse,
        *,
        reason: Literal["execution_failed", "deadline", "cancelled", "infrastructure"],
    ) -> V9ConfirmationFailResponse:
        """Close one private v9 lease without touching canonical fail routes."""
        url = f"{self._base}{_PREFIX}/v9-confirmation/bundle/{job.bundle_id}/fail"
        last_error: httpx.HTTPError | None = None
        for delay in (0.0, 0.25, 1.0):
            if delay:
                await asyncio.sleep(delay)
            requested_at = datetime.now(UTC)
            nonce = uuid4()
            payload = V9ConfirmationFailRequest(
                validator_hotkey=self._config.validator_hotkey,
                ticket_id=job.ticket_id,
                reason=reason,
                nonce=nonce,
                requested_at=requested_at,
                signature=sign_v9_confirmation_fail(
                    self._keypair,
                    validator_hotkey=self._config.validator_hotkey,
                    bundle_id=job.bundle_id,
                    ticket_id=job.ticket_id,
                    reason=reason,
                    nonce=nonce,
                    requested_at=requested_at,
                ),
            )
            try:
                response = await self._client.post(
                    url,
                    headers=self._headers,
                    json=payload.model_dump(mode="json"),
                )
            except httpx.HTTPError as error:
                last_error = error
                continue
            if response.status_code == 200:
                try:
                    failed = V9ConfirmationFailResponse.model_validate(response.json())
                except (ValidationError, ValueError) as error:
                    raise PlatformError(
                        "v9 confirmation failure response was invalid"
                    ) from error
                if (
                    failed.bundle_id != job.bundle_id
                    or failed.ticket_id != job.ticket_id
                ):
                    raise PlatformError(
                        "v9 confirmation failure response identity mismatch"
                    )
                return failed
            if response.status_code not in {408, 429} and response.status_code < 500:
                raise PlatformError(
                    "v9 confirmation failure rejected "
                    f"({response.status_code}): {response.text[:200]}"
                )
        if last_error is not None:
            raise PlatformError(
                f"v9 confirmation failure hand-back failed: {last_error}"
            ) from last_error
        raise PlatformError("v9 confirmation failure hand-back exhausted retries")

    async def exchange_inference_grant(
        self, grant_id: UUID, broker_public_key: str, exchange_url: str
    ) -> InferenceExchangeResponse:
        """Authorize one trusted broker key for the exact live ticket grant.

        Exchange is safe to retry: every attempt is freshly signed and the
        platform rotates the same live grant onto the same broker key. A brief
        relay restart must not consume the miner's validation attempt before a
        benchmark has even started.
        """
        # The platform may serve its inference plane on a different public
        # hostname than the API host this validator posts jobs and scores to
        # (DITTO_INFERENCE_PUBLIC_BASE_URL vs the API base). Accept either, and
        # nothing else: this stays a two-entry allowlist, so a malicious ticket
        # still cannot point a grant exchange at a host of its choosing.
        permitted = {
            f"{base}/api/v1/inference/exchange"
            for base in (self._base, self._inference_base)
        }
        verified_url = exchange_url.rstrip("/")
        if verified_url not in permitted:
            raise PlatformError("ticket inference exchange URL is not the platform")
        attempts = len(_INFERENCE_EXCHANGE_RETRY_DELAYS) + 1
        for attempt in range(attempts):
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
                    verified_url,
                    headers=self._headers,
                    json=payload.model_dump(mode="json"),
                )
            except httpx.HTTPError as error:
                if attempt < attempts - 1:
                    await asyncio.sleep(_INFERENCE_EXCHANGE_RETRY_DELAYS[attempt])
                    continue
                raise PlatformInfrastructureError(
                    f"inference exchange failed after {attempts} attempts: {error}"
                ) from error
            if response.status_code == 200:
                exchange = InferenceExchangeResponse.model_validate(response.json())
                return exchange.model_copy(update=_inference_budget_evidence(response))
            retryable = (
                response.status_code in {408, 429} or response.status_code >= 500
            )
            if retryable and attempt < attempts - 1:
                await asyncio.sleep(_INFERENCE_EXCHANGE_RETRY_DELAYS[attempt])
                continue
            message = (
                f"inference exchange rejected ({response.status_code}): "
                f"{response.text[:200]}"
            )
            if retryable:
                raise PlatformInfrastructureError(
                    f"{message} after {attempts} attempts"
                )
            raise PlatformError(message)
        raise AssertionError("inference exchange retry loop did not return")

    async def report_ticket_failed(
        self,
        job: JobResponse,
        reason: FailJobReason,
        failure_detail: str | None = None,
    ) -> FailJobResponse:
        """Hand a failed ticket back so the platform reissues a fresh lease.

        POST /validator/job/fail closes the still-live lease for
        ``(job.agent_id, job.deadline)`` immediately (rather than waiting for it
        to expire) so the next :meth:`request_job` mints a brand-new ticket
        instead of resuming the failed attempt. Raises :class:`PlatformError` on
        any non-200; callers MUST treat this as best-effort and never let a
        failed report crash the scoring sweep — an old platform without this
        endpoint just leaves the ticket to expire on its own, exactly as before.

        ``failure_detail`` is the reporter's own code or diagnostic message
        behind ``reason`` (see :func:`ditto.validator.errors.failure_detail`). It
        is optional and unsigned, exactly as ``reason`` is, so a platform
        predating the field ignores it and a validator predating it simply omits
        it. Bounded by the wire model, and truncated before it gets here.

        The bound moved 200 -> 4096, and the fleet does not upgrade atomically,
        so this method now also handles the one skew that widening creates: a
        detail this validator considers legal that the platform on the other end
        still rejects. See :meth:`_post_job_fail`.
        """
        requested_at = datetime.now(UTC)
        nonce = uuid4()
        payload = FailJobRequest(
            validator_hotkey=self._config.validator_hotkey,
            agent_id=job.agent_id,
            ticket_deadline=job.deadline,
            reason=reason,
            failure_detail=failure_detail,
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
        resp = await self._post_job_fail(payload)
        if (
            resp.status_code == 422
            and payload.failure_detail is not None
            and len(payload.failure_detail) > LEGACY_FAILURE_DETAIL_MAX_LENGTH
        ):
            # Version skew, the only direction that can actually break. A
            # platform that has not taken the widening still caps this field at
            # 200 and answers 422 — and a 422 here is not a lost field, it is a
            # lost *hand-back*: the lease stays live until its deadline and the
            # slot sits idle, which is the silent expiry `failure_detail` was
            # introduced to eliminate. Trading the tail of one message for the
            # whole report is unambiguously the right side of that trade.
            #
            # Safe to replay with the same nonce and signature: a 422 is request
            # validation, raised before the endpoint body runs, so the nonce was
            # never consumed. And the signature does not cover `failure_detail`
            # (it never has — the field is unsigned by design), so shortening it
            # leaves the signed payload byte-identical. Nothing needs re-signing.
            #
            # Deliberately not a general retry: it fires once, only on 422, and
            # only when the detail is long enough for the length bound to be a
            # plausible cause. Any other 422 falls through to the raise below
            # after one wasted round trip.
            logger.info(
                "job fail report rejected 422 with a %d-char detail; retrying "
                "at the legacy %d-char bound (platform predates the widening) "
                "agent=%s",
                len(payload.failure_detail),
                LEGACY_FAILURE_DETAIL_MAX_LENGTH,
                job.agent_id,
            )
            resp = await self._post_job_fail(
                payload.model_copy(
                    update={
                        "failure_detail": truncate_failure_detail(
                            payload.failure_detail, LEGACY_FAILURE_DETAIL_MAX_LENGTH
                        )
                    }
                )
            )
        if resp.status_code != 200:
            raise PlatformError(
                f"job fail report rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return FailJobResponse.model_validate(resp.json())

    async def _post_job_fail(self, payload: FailJobRequest) -> httpx.Response:
        """POST one hand-back attempt, returning the response uninterpreted.

        Split out so the legacy-bound retry in :meth:`report_ticket_failed`
        sends through exactly the same serialization as the first attempt — in
        particular ``exclude_none``, which is what keeps a detail-free report
        byte-identical to the pre-``failure_detail`` wire format.
        """
        try:
            return await self._client.post(
                f"{self._base}{_PREFIX}/job/fail",
                headers=self._headers,
                # exclude_none drops only failure_detail — every other field on
                # this model is required and non-None — so a report with no
                # detail is byte-identical to what this client sent before the
                # field existed, and an older platform sees no new key at all.
                json=payload.model_dump(mode="json", exclude_none=True),
            )
        except httpx.HTTPError as e:
            raise PlatformError(f"job fail report failed: {e}") from e

    async def request_top5_confirmation_job(
        self, *, slot_id: str
    ) -> JobResponse | None:
        """Ask Platform to route one authoritative continual retest to a slot.

        The validator deliberately supplies no champion or cohort member. Those
        are durable scheduling decisions and must be derived in the same
        transaction that reserves the lease; a local ledger projection can be
        stale by the time this request reaches Platform.
        """
        url = f"{self._base}{_PREFIX}/top5-confirmation-job"
        requested_at = datetime.now(UTC)
        nonce = uuid4()
        payload = Top5ConfirmationJobRequest(
            validator_hotkey=self._config.validator_hotkey,
            slot_id=slot_id,
            nonce=nonce,
            requested_at=requested_at,
            signature=sign_top5_confirmation_job_request(
                self._keypair,
                validator_hotkey=self._config.validator_hotkey,
                slot_id=slot_id,
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
        if resp.status_code == 204:
            return None
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
        ledger = LedgerResponse.model_validate(resp.json())
        if ledger.v9_confirmation_mode == "enforce" and any(
            entry.bench_version == 9 and entry.v9_confirmation is None
            for entry in ledger.entries
        ):
            raise PlatformError(
                "v9 enforce ledger contained a row without full confirmation"
            )
        if ledger.v9_confirmation_mode is None and any(
            entry.v9_confirmation is not None for entry in ledger.entries
        ):
            raise PlatformError(
                "ledger carried v9 confirmation receipts without enforce marker"
            )
        invalid = [
            entry.agent_id for entry in ledger.entries if not verify_ledger_entry(entry)
        ]
        if invalid:
            sample = ", ".join(str(agent_id) for agent_id in invalid[:3])
            raise PlatformError(
                "ledger score proof verification failed for "
                f"{len(invalid)} entr{'y' if len(invalid) == 1 else 'ies'}: {sample}"
            )
        return ledger

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
            base_evidence_sha256=report.base_evidence_sha256,
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
