"""Default-off shadow coding worker with durable publication recovery."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from ditto.api_models.coding import (
    CodingAuthoringEvidence,
    CodingAuthoringLeaseResponse,
    CodingGradingLeaseResponse,
    CodingRunEvidence,
    CodingRunManifest,
    CodingTaskEvidence,
    SubmitCodingAuthoringFreezeRequest,
    SubmitCodingAuthoringFreezeResponse,
    SubmitCodingShadowResultResponse,
)
from ditto.api_models.coding_claims import CodingClaimResponse
from ditto.api_models.coding_harness import CodingHarnessLaunchResponse
from ditto.validator.coding_attempt import (
    CodingAttemptCoordinator,
    CodingAttemptIntegrityError,
    CodingAttemptRuntime,
    CodingAttemptTicket,
    CodingAuthoringOutcome,
)
from ditto.validator.coding_publication import (
    CodingPublicationClient,
    PreparedCodingPublication,
    PublicationArtifact,
    PublicationRecord,
)
from ditto.validator.coding_supervisor import (
    CodingSupervisorRecovery,
    validate_coding_grant_preflight,
)
from ditto.validator.errors import PlatformInfrastructureError

logger = logging.getLogger(__name__)
_LOCKED_POLICY_SHA256 = (
    "b2f38d9f6b5484e9a056d74be4dc0250912f05c9e51512801b590dff934a41d6"
)


class DurableCodingAttemptPlatform:
    """Adapt PlatformClient to the coordinator with an exact-byte outbox."""

    def __init__(self, platform: Any, publication: CodingPublicationClient) -> None:
        self._platform = platform
        self._publication = publication

    async def request_coding_harness_launch(
        self, ticket_id: UUID
    ) -> CodingHarnessLaunchResponse:
        return await self._platform.request_coding_harness_launch(ticket_id)

    async def request_coding_authoring_lease(
        self, ticket_id: UUID
    ) -> CodingAuthoringLeaseResponse:
        return await self._platform.request_coding_authoring_lease(ticket_id)

    async def request_coding_grading_lease(
        self, **authority: Any
    ) -> CodingGradingLeaseResponse:
        return await self._platform.request_coding_grading_lease(**authority)

    async def submit_coding_authoring_freeze(
        self,
        agent_id: UUID,
        *,
        bench_version: int,
        run_row_id: UUID,
        ticket_id: UUID,
        ticket_deadline: datetime,
        coding_run_id: str,
        agent_artifact_sha256: str,
        screened_image_sha256: str,
        run_manifest_sha256: str,
        task_set_manifest_sha256: str,
        evidence: CodingAuthoringEvidence,
        authoring_transcript_object_key: str,
        authoring_transcript_bytes: int,
        authoring_event_count: int,
        frozen_submission_object_key: str,
    ) -> SubmitCodingAuthoringFreezeResponse:
        prepared = self._platform.prepare_coding_authoring_freeze(
            agent_id,
            bench_version=bench_version,
            run_row_id=run_row_id,
            ticket_id=ticket_id,
            ticket_deadline=ticket_deadline,
            coding_run_id=coding_run_id,
            agent_artifact_sha256=agent_artifact_sha256,
            screened_image_sha256=screened_image_sha256,
            run_manifest_sha256=run_manifest_sha256,
            task_set_manifest_sha256=task_set_manifest_sha256,
            evidence=evidence,
            authoring_transcript_object_key=authoring_transcript_object_key,
            authoring_transcript_bytes=authoring_transcript_bytes,
            authoring_event_count=authoring_event_count,
            frozen_submission_object_key=frozen_submission_object_key,
        )
        response = await self.publish(prepared)
        if not isinstance(response, SubmitCodingAuthoringFreezeResponse):
            raise PlatformInfrastructureError(
                "coding authoring publication returned the wrong response"
            )
        return response

    async def submit_coding_shadow_result(
        self,
        agent_id: UUID,
        *,
        bench_version: int,
        run_row_id: UUID,
        ticket_id: UUID,
        ticket_deadline: datetime,
        agent_artifact_sha256: str,
        screened_image_sha256: str,
        run_manifest: CodingRunManifest,
        evidence: CodingRunEvidence,
        task_evidence: list[CodingTaskEvidence],
    ) -> SubmitCodingShadowResultResponse:
        prepared = self._platform.prepare_coding_shadow_result(
            agent_id,
            bench_version=bench_version,
            run_row_id=run_row_id,
            ticket_id=ticket_id,
            ticket_deadline=ticket_deadline,
            agent_artifact_sha256=agent_artifact_sha256,
            screened_image_sha256=screened_image_sha256,
            run_manifest=run_manifest,
            evidence=evidence,
            task_evidence=task_evidence,
        )
        response = await self.publish(prepared)
        if not isinstance(response, SubmitCodingShadowResultResponse):
            raise PlatformInfrastructureError(
                "coding terminal publication returned the wrong response"
            )
        return response

    async def publish(
        self, prepared: PreparedCodingPublication
    ) -> SubmitCodingAuthoringFreezeResponse | SubmitCodingShadowResultResponse:
        _, request = await self._publication.prepare(
            ticket_id=str(prepared.ticket_id),
            stage=prepared.stage,
            authority=prepared.authority,
            body=prepared.body,
        )
        _validate_artifact(request, prepared.body)
        (
            accepted,
            acknowledgement,
        ) = await self._platform.publish_prepared_coding_publication(prepared)
        stored = await self._publication.acknowledge(
            ticket_id=str(prepared.ticket_id),
            stage=prepared.stage,
            request_sha256=request.sha256,
            body=acknowledgement,
        )
        _validate_artifact(stored, acknowledgement)
        return accepted


class CodingWorkerRuntime(CodingAttemptRuntime, Protocol):
    async def recover(
        self,
        *,
        ticket_id: UUID,
        coding_run_id: str,
        deadline: datetime,
    ) -> CodingSupervisorRecovery: ...


class CodingShadowWorker:
    """Claim and execute at most one shadow coding ticket per stable instance."""

    def __init__(
        self,
        *,
        platform: Any,
        runtime: CodingWorkerRuntime,
        publication: CodingPublicationClient,
        instance_id: str,
        poll_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not instance_id
            or len(instance_id.encode()) > 128
            or any(
                character.isspace() or not character.isprintable()
                for character in instance_id
            )
            or not 1 <= poll_seconds <= 300
        ):
            raise ValueError("coding shadow worker configuration is invalid")
        self._platform = platform
        self._runtime = runtime
        self._publication = publication
        self._instance_id = instance_id
        self._poll_seconds = poll_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._last_now = self._clock()
        if self._last_now.tzinfo is None or self._last_now.utcoffset() is None:
            raise ValueError("coding shadow worker clock is invalid")
        self._last_now = self._last_now.astimezone(UTC)
        self.busy = False
        self._drain_requested: asyncio.Event | None = None
        self._durable = DurableCodingAttemptPlatform(platform, publication)
        self._coordinator = CodingAttemptCoordinator(
            platform=self._durable,
            runtime=runtime,
            clock=self._now,
        )

    async def run_forever(
        self,
        stop: asyncio.Event,
        *,
        drain_requested: asyncio.Event,
    ) -> None:
        self._drain_requested = drain_requested
        while not stop.is_set():
            if drain_requested.is_set() and not self.busy:
                await _wait_or_stop(stop, self._poll_seconds)
                continue
            self.busy = True
            try:
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "shadow coding worker attempt failed type=%s",
                    type(error).__name__,
                )
                worked = False
            finally:
                self.busy = False
            if not worked:
                await _wait_or_stop(stop, self._poll_seconds)

    async def run_once(self) -> bool:
        # Prove the scorer-side outbox/supervisor host exists before taking any
        # Platform work. The host constructs both services atomically.
        await self._publication.pending(limit=1)
        if self._drain_requested is not None and self._drain_requested.is_set():
            return False
        claim = await self._platform.claim_next_coding_ticket(self._instance_id)
        if claim is None:
            return False
        _validate_claim(claim, self._instance_id, now=self._now())
        if claim.claim_started_at is None:
            authoring_lease = await self._platform.request_coding_authoring_lease(
                claim.ticket_id
            )
            harness = await self._platform.request_coding_harness_launch(
                claim.ticket_id
            )
            self._coordinator.validate_preflight(
                _ticket(claim),
                authoring_lease=authoring_lease,
                harness=harness,
            )
            preflight_now = self._now()
            if harness.expires_at <= preflight_now or any(
                capability.expires_at <= preflight_now
                for capability in authoring_lease.capabilities
            ):
                raise CodingAttemptIntegrityError(
                    "coding preflight capability is already expired"
                )
            offer = await self._platform.request_coding_inference_grant(claim.ticket_id)
            validate_coding_grant_preflight(authoring_lease, offer)
            if (
                offer.inference_grant_sha256 != _LOCKED_POLICY_SHA256
                or offer.expires_at <= self._now()
            ):
                raise CodingAttemptIntegrityError(
                    "coding preflight grant policy or lifetime is invalid"
                )
            claim = await self._platform.start_coding_ticket_claim(claim)
            started_now = self._now()
            _validate_claim(
                claim,
                self._instance_id,
                started=True,
                now=started_now,
            )
            if harness.expires_at <= started_now or any(
                capability.expires_at <= started_now
                for capability in authoring_lease.capabilities
            ) or offer.expires_at <= started_now:
                raise CodingAttemptIntegrityError(
                    "coding claim started after preflight authority expired"
                )
            await self._with_heartbeat(
                claim,
                lambda: self._execute_new(
                    claim,
                    authoring_lease=authoring_lease,
                    harness=harness,
                ),
            )
            return True
        return await self._with_heartbeat(
            claim,
            lambda: self._recover_started(claim),
        )

    async def _recover_started(self, claim: CodingClaimResponse) -> bool:
        recovery = await self._runtime.recover(
            ticket_id=claim.ticket_id,
            coding_run_id=claim.coding_run_id,
            deadline=claim.ticket_deadline,
        )
        if recovery.state == "released":
            return True
        if recovery.state == "terminal_pending":
            await self._publish_recovery(claim, recovery)
            await self._require_released(claim)
            return True
        if recovery.state in {"authoring_pending", "authoring_published"}:
            authoring, freeze = await self._recover_authoring(claim, recovery)
            lease = await self._platform.request_coding_authoring_lease(claim.ticket_id)
            _validate_recovered_authoring(claim, lease, authoring, freeze)
            await self._coordinator.resume_after_authoring(
                _ticket(claim),
                authoring_lease=lease,
                authoring=authoring,
                freeze=freeze,
            )
            await self._require_released(claim)
            return True
        logger.warning(
            "shadow coding recovery is non-rerunnable state=%s",
            recovery.state,
        )
        return False

    async def _execute_new(
        self,
        claim: CodingClaimResponse,
        *,
        authoring_lease: CodingAuthoringLeaseResponse,
        harness: CodingHarnessLaunchResponse,
    ) -> None:
        await self._coordinator.execute_prepared(
            _ticket(claim),
            authoring_lease=authoring_lease,
            harness=harness,
        )
        await self._require_released(claim)

    async def _require_released(self, claim: CodingClaimResponse) -> None:
        terminal = await self._runtime.recover(
            ticket_id=claim.ticket_id,
            coding_run_id=claim.coding_run_id,
            deadline=claim.ticket_deadline,
        )
        if terminal.state != "released":
            raise CodingAttemptIntegrityError(
                "coding terminal acknowledgement is not durable"
            )

    async def _publish_recovery(
        self,
        claim: CodingClaimResponse,
        recovery: CodingSupervisorRecovery,
    ) -> SubmitCodingAuthoringFreezeResponse | SubmitCodingShadowResultResponse:
        if recovery.publication_stage is None or recovery.request_sha256 is None:
            raise CodingAttemptIntegrityError(
                "coding recovery omitted publication authority"
            )
        record = await self._publication.lookup(
            ticket_id=str(claim.ticket_id),
            stage=recovery.publication_stage,
        )
        if record.request.sha256 != recovery.request_sha256:
            raise CodingAttemptIntegrityError(
                "coding recovery publication digest disagrees"
            )
        body = await self._publication.open(
            record_id=record.record_id,
            stage=record.stage,
            expected=record.request,
        )
        prepared = PreparedCodingPublication(
            stage=record.stage,
            ticket_id=record.ticket_id,
            agent_id=record.authority.agent_id,
            authority=record.authority,
            body=body,
        )
        if record.acknowledgement is not None:
            acknowledgement = await self._publication.open(
                record_id=record.record_id,
                stage=record.stage,
                expected=record.acknowledgement,
                acknowledgement=True,
            )
            return _parse_acknowledgement(record, acknowledgement)
        return await self._durable.publish(prepared)

    async def _recover_authoring(
        self,
        claim: CodingClaimResponse,
        recovery: CodingSupervisorRecovery,
    ) -> tuple[CodingAuthoringOutcome, SubmitCodingAuthoringFreezeResponse]:
        accepted = await self._publish_recovery(claim, recovery)
        if not isinstance(accepted, SubmitCodingAuthoringFreezeResponse):
            raise CodingAttemptIntegrityError(
                "coding recovery returned a non-authoring acknowledgement"
            )
        record = await self._publication.lookup(
            ticket_id=str(claim.ticket_id),
            stage="authoring_freeze",
        )
        body = await self._publication.open(
            record_id=record.record_id,
            stage=record.stage,
            expected=record.request,
        )
        try:
            request = SubmitCodingAuthoringFreezeRequest.model_validate_json(body)
            outcome = CodingAuthoringOutcome(
                evidence=request.evidence,
                authoring_transcript_object_key=(
                    request.authoring_transcript_object_key
                ),
                authoring_transcript_bytes=request.authoring_transcript_bytes,
                authoring_event_count=request.authoring_event_count,
                frozen_submission_object_key=(request.frozen_submission_object_key),
                capabilities_revoked=True,
                authoring_environment_destroyed=True,
            )
        except ValueError as error:
            raise CodingAttemptIntegrityError(
                "coding recovery authoring request is invalid"
            ) from error
        return outcome, accepted

    async def _with_heartbeat(
        self,
        claim: CodingClaimResponse,
        operation: Callable[[], Coroutine[Any, Any, Any]],
    ) -> Any:
        done = asyncio.Event()

        async def heartbeat() -> None:
            current = claim
            retrying = False
            while not done.is_set():
                delay = (
                    5.0
                    if retrying
                    else max(
                        1.0,
                        min(
                            30.0,
                            (current.claim_expires_at - self._now()).total_seconds()
                            / 3,
                        ),
                    )
                )
                try:
                    await asyncio.wait_for(done.wait(), timeout=delay)
                    return
                except TimeoutError:
                    try:
                        current = await self._platform.heartbeat_coding_ticket_claim(
                            current
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if (
                            current.claim_expires_at - self._now()
                        ).total_seconds() <= 15:
                            raise
                        retrying = True
                        continue
                    retrying = False
                    _validate_claim(
                        current,
                        self._instance_id,
                        started=True,
                        now=self._now(),
                    )

        async with asyncio.TaskGroup() as group:
            heartbeat_task = group.create_task(heartbeat())
            operation_task: asyncio.Task[Any] = group.create_task(operation())
            try:
                result = await operation_task
            finally:
                done.set()
            await heartbeat_task
        return result

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise CodingAttemptIntegrityError("coding shadow worker clock is invalid")
        value = value.astimezone(UTC)
        if value < self._last_now:
            raise CodingAttemptIntegrityError(
                "coding shadow worker clock moved backwards"
            )
        self._last_now = value
        return value


def _ticket(claim: CodingClaimResponse) -> CodingAttemptTicket:
    return CodingAttemptTicket(
        agent_id=claim.agent_id,
        bench_version=claim.bench_version,
        run_row_id=claim.run_row_id,
        ticket_id=claim.ticket_id,
        ticket_deadline=claim.ticket_deadline,
        agent_artifact_sha256=claim.agent_artifact_sha256,
        screened_image_sha256=claim.screened_image_sha256,
        weight_eligible=False,
    )


def _validate_claim(
    claim: CodingClaimResponse,
    instance_id: str,
    *,
    started: bool | None = None,
    now: datetime,
) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise CodingAttemptIntegrityError("coding claim clock is invalid")
    now = now.astimezone(UTC)
    if (
        claim.instance_id != instance_id
        or claim.weight_eligible is not False
        or claim.claim_expires_at <= now
        or claim.ticket_deadline <= now
        or (started is True and claim.claim_started_at is None)
    ):
        raise CodingAttemptIntegrityError("coding claim authority is inactive")


def _validate_recovered_authoring(
    claim: CodingClaimResponse,
    lease: CodingAuthoringLeaseResponse,
    authoring: CodingAuthoringOutcome,
    freeze: SubmitCodingAuthoringFreezeResponse,
) -> None:
    if (
        lease.ticket_id != claim.ticket_id
        or lease.coding_run_id != claim.coding_run_id
        or lease.run_manifest_sha256 != claim.run_manifest_sha256
        or lease.task_set_manifest_sha256 != claim.task_set_manifest_sha256
        or lease.run_manifest.agent_id != str(claim.agent_id)
        or lease.run_manifest.agent_artifact_sha256 != claim.agent_artifact_sha256
        or freeze.ticket_id != claim.ticket_id
        or freeze.run_row_id != claim.run_row_id
        or freeze.agent_id != claim.agent_id
        or authoring.evidence.model.inference_grant_sha256
        != lease.run_manifest.inference_grant_sha256
    ):
        raise CodingAttemptIntegrityError(
            "coding recovered authoring authority disagrees with claim"
        )


def _parse_acknowledgement(
    record: PublicationRecord,
    body: bytes,
) -> SubmitCodingAuthoringFreezeResponse | SubmitCodingShadowResultResponse:
    try:
        value: SubmitCodingAuthoringFreezeResponse | SubmitCodingShadowResultResponse
        if record.stage == "authoring_freeze":
            value = SubmitCodingAuthoringFreezeResponse.model_validate_json(body)
        else:
            value = SubmitCodingShadowResultResponse.model_validate_json(body)
    except ValueError as error:
        raise CodingAttemptIntegrityError(
            "coding publication acknowledgement is invalid"
        ) from error
    if (
        value.ticket_id != record.ticket_id
        or value.agent_id != record.authority.agent_id
    ):
        raise CodingAttemptIntegrityError(
            "coding publication acknowledgement authority disagrees"
        )
    if isinstance(value, SubmitCodingAuthoringFreezeResponse):
        if (
            value.run_row_id != record.authority.run_row_id
            or value.coding_run_id != record.authority.coding_run_id
            or value.authoring_evidence_sha256 != record.authority.evidence_sha256
        ):
            raise CodingAttemptIntegrityError(
                "coding authoring acknowledgement authority disagrees"
            )
    elif (
        value.run_row_id != record.authority.run_row_id
        or value.coding_run_id != record.authority.coding_run_id
    ):
        raise CodingAttemptIntegrityError(
            "coding terminal acknowledgement authority disagrees"
        )
    return value


def _validate_artifact(artifact: PublicationArtifact, body: bytes) -> None:
    if (
        artifact.size_bytes != len(body)
        or artifact.sha256 != hashlib.sha256(body).hexdigest()
    ):
        raise PlatformInfrastructureError(
            "coding publication artifact identity is invalid"
        )


async def _wait_or_stop(stop: asyncio.Event, delay: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=delay)


__all__ = [
    "CodingShadowWorker",
    "DurableCodingAttemptPlatform",
]
