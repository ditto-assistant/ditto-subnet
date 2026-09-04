"""Shadow-only coordinator used by the default-off coding worker."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from ditto.api_models.coding import (
    CodingAuthoringEvidence,
    CodingAuthoringLeaseResponse,
    CodingGradingLeaseResponse,
    CodingModelUsageStatus,
    CodingRunEvidence,
    CodingRunManifest,
    CodingTaskEvidence,
    SubmitCodingAuthoringFreezeResponse,
    SubmitCodingShadowResultResponse,
    canonical_digest,
    coding_authoring_evidence_digest,
)
from ditto.api_models.coding_harness import CodingHarnessLaunchResponse
from ditto.validator.coding_failure import (
    CodingFailureCode,
    CodingFailureStage,
    build_coding_failure_task_evidence,
)
from ditto.validator.coding_terminal import (
    CodingTerminalEvidenceError,
    build_coding_run_evidence,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CodingAttemptError(Exception):
    """Base class for local coding-attempt orchestration failures."""


class CodingAttemptExpiredError(CodingAttemptError):
    """The ticket cannot begin or enter grading after its deadline."""


class CodingAttemptIntegrityError(CodingAttemptError):
    """A local runtime or lease violated immutable attempt authority."""


@dataclass(frozen=True)
class CodingAttemptTicket:
    agent_id: UUID
    bench_version: int
    run_row_id: UUID
    ticket_id: UUID
    ticket_deadline: datetime
    agent_artifact_sha256: str
    screened_image_sha256: str
    claim_instance_id: str
    weight_eligible: Literal[False] = False

    def __post_init__(self) -> None:
        identifiers = (self.agent_id, self.run_row_id, self.ticket_id)
        if (
            not all(isinstance(value, UUID) for value in identifiers)
            or any(value.int == 0 for value in identifiers)
            or type(self.bench_version) is not int
            or self.bench_version < 7
            or not isinstance(self.ticket_deadline, datetime)
            or self.ticket_deadline.tzinfo is None
            or self.ticket_deadline.utcoffset() is None
            or not isinstance(self.agent_artifact_sha256, str)
            or not _SHA256_RE.fullmatch(self.agent_artifact_sha256)
            or not isinstance(self.screened_image_sha256, str)
            or not _SHA256_RE.fullmatch(self.screened_image_sha256)
            or not isinstance(self.claim_instance_id, str)
            or not 1 <= len(self.claim_instance_id.encode()) <= 128
            or any(
                character.isspace()
                or unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co"}
                for character in self.claim_instance_id
            )
            or self.weight_eligible is not False
        ):
            raise CodingAttemptIntegrityError("coding attempt ticket is invalid")


@dataclass(frozen=True)
class CodingAuthoringOutcome:
    evidence: CodingAuthoringEvidence
    authoring_transcript_object_key: str
    authoring_transcript_bytes: int
    authoring_event_count: int
    frozen_submission_object_key: str
    capabilities_revoked: Literal[True]
    authoring_environment_destroyed: Literal[True]

    def __post_init__(self) -> None:
        usage = self.evidence.model.usage_status
        invoked = usage is not CodingModelUsageStatus.NOT_INVOKED
        if (
            usage
            not in {
                CodingModelUsageStatus.COMPLETE,
                CodingModelUsageStatus.NOT_INVOKED,
                CodingModelUsageStatus.PROVIDER_FAILURE,
            }
            or not self.evidence.protected_paths_intact
            or type(self.evidence.changed_path_count) is not int
            or self.evidence.changed_path_count < 0
            or type(self.authoring_transcript_bytes) is not int
            or type(self.authoring_event_count) is not int
            or not 0 <= self.authoring_transcript_bytes <= 512 << 20
            or not 0 <= self.authoring_event_count <= 1_000
            or (
                invoked
                and (
                    self.authoring_transcript_bytes == 0
                    or self.authoring_event_count == 0
                )
            )
            or self.authoring_transcript_object_key
            != f"sha256/{self.evidence.authoring_transcript_sha256}"
            or self.frozen_submission_object_key
            != f"sha256/{self.evidence.frozen_patch_sha256}"
            or self.capabilities_revoked is not True
            or self.authoring_environment_destroyed is not True
        ):
            raise CodingAttemptIntegrityError(
                "coding authoring outcome is not gradeable"
            )


@dataclass(frozen=True)
class CodingGradingOutcome:
    task_evidence: tuple[CodingTaskEvidence, ...]
    grading_environment_destroyed: Literal[True]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.task_evidence, tuple)
            or not 1 <= len(self.task_evidence) <= 100
            or self.grading_environment_destroyed is not True
        ):
            raise CodingAttemptIntegrityError("coding grading outcome is incomplete")


class CodingAttemptPlatform(Protocol):
    async def request_coding_harness_launch(
        self,
        ticket_id: UUID,
    ) -> CodingHarnessLaunchResponse: ...

    async def request_coding_authoring_lease(
        self,
        ticket_id: UUID,
    ) -> CodingAuthoringLeaseResponse: ...

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
    ) -> SubmitCodingAuthoringFreezeResponse: ...

    async def request_coding_grading_lease(
        self,
        *,
        agent_id: UUID,
        run_row_id: UUID,
        ticket_id: UUID,
        freeze_id: UUID,
        claim_instance_id: str,
        authoring_evidence_sha256: str,
        expected_frozen_patch_sha256: str,
    ) -> CodingGradingLeaseResponse: ...

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
    ) -> SubmitCodingShadowResultResponse: ...


class CodingAttemptRuntime(Protocol):
    async def author(
        self,
        lease: CodingAuthoringLeaseResponse,
        harness: CodingHarnessLaunchResponse,
    ) -> CodingAuthoringOutcome: ...

    async def grade(
        self,
        lease: CodingGradingLeaseResponse,
        authoring: CodingAuthoringOutcome,
    ) -> CodingGradingOutcome: ...

    async def abort_authoring(
        self,
        lease: CodingAuthoringLeaseResponse,
    ) -> None: ...

    async def abort_grading(
        self,
        lease: CodingGradingLeaseResponse,
    ) -> None: ...


class CodingAttemptCoordinator:
    """Enforce freeze-before-grader ordering for one gradeable shadow task."""

    def __init__(
        self,
        *,
        platform: CodingAttemptPlatform,
        runtime: CodingAttemptRuntime,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._platform = platform
        self._runtime = runtime
        self._clock = clock or (lambda: datetime.now(UTC))

    async def execute(
        self,
        ticket: CodingAttemptTicket,
    ) -> SubmitCodingShadowResultResponse:
        self._require_active(ticket, phase="authoring")
        authoring_lease = await self._platform.request_coding_authoring_lease(
            ticket.ticket_id
        )
        self._validate_authoring_lease(ticket, authoring_lease)
        harness = await self._platform.request_coding_harness_launch(ticket.ticket_id)
        return await self._execute_prepared(
            ticket,
            authoring_lease=authoring_lease,
            harness=harness,
            check_active=False,
        )

    async def execute_prepared(
        self,
        ticket: CodingAttemptTicket,
        *,
        authoring_lease: CodingAuthoringLeaseResponse,
        harness: CodingHarnessLaunchResponse,
    ) -> SubmitCodingShadowResultResponse:
        """Execute after non-mutating lease, harness, and grant preflight."""

        return await self._execute_prepared(
            ticket,
            authoring_lease=authoring_lease,
            harness=harness,
            check_active=True,
        )

    def validate_preflight(
        self,
        ticket: CodingAttemptTicket,
        *,
        authoring_lease: CodingAuthoringLeaseResponse,
        harness: CodingHarnessLaunchResponse,
    ) -> None:
        """Validate every non-executing authority before committing start."""

        self._require_active(ticket, phase="authoring")
        self._validate_authoring_lease(ticket, authoring_lease)
        self._validate_harness_launch(ticket, authoring_lease, harness)

    async def _execute_prepared(
        self,
        ticket: CodingAttemptTicket,
        *,
        authoring_lease: CodingAuthoringLeaseResponse,
        harness: CodingHarnessLaunchResponse,
        check_active: bool,
    ) -> SubmitCodingShadowResultResponse:
        if check_active:
            self._require_active(ticket, phase="authoring")

        self._validate_authoring_lease(ticket, authoring_lease)
        self._validate_harness_launch(ticket, authoring_lease, harness)
        try:
            authoring = await self._runtime.author(authoring_lease, harness)
        except BaseException:
            await self._abort_authoring(authoring_lease)
            raise
        if (
            authoring.evidence.model.inference_grant_sha256
            != authoring_lease.run_manifest.inference_grant_sha256
        ):
            raise CodingAttemptIntegrityError(
                "authoring evidence disagrees with inference authority"
            )

        freeze = await self._platform.submit_coding_authoring_freeze(
            ticket.agent_id,
            bench_version=ticket.bench_version,
            run_row_id=ticket.run_row_id,
            ticket_id=ticket.ticket_id,
            ticket_deadline=ticket.ticket_deadline,
            coding_run_id=authoring_lease.coding_run_id,
            agent_artifact_sha256=ticket.agent_artifact_sha256,
            screened_image_sha256=ticket.screened_image_sha256,
            run_manifest_sha256=authoring_lease.run_manifest_sha256,
            task_set_manifest_sha256=authoring_lease.task_set_manifest_sha256,
            evidence=authoring.evidence,
            authoring_transcript_object_key=(authoring.authoring_transcript_object_key),
            authoring_transcript_bytes=authoring.authoring_transcript_bytes,
            authoring_event_count=authoring.authoring_event_count,
            frozen_submission_object_key=authoring.frozen_submission_object_key,
        )

        return await self.resume_after_authoring(
            ticket,
            authoring_lease=authoring_lease,
            authoring=authoring,
            freeze=freeze,
        )

    async def resume_after_authoring(
        self,
        ticket: CodingAttemptTicket,
        *,
        authoring_lease: CodingAuthoringLeaseResponse,
        authoring: CodingAuthoringOutcome,
        freeze: SubmitCodingAuthoringFreezeResponse,
    ) -> SubmitCodingShadowResultResponse:
        """Continue from one durably published freeze without rerunning authoring."""

        self._validate_authoring_lease(ticket, authoring_lease)
        if (
            authoring.evidence.model.inference_grant_sha256
            != authoring_lease.run_manifest.inference_grant_sha256
        ):
            raise CodingAttemptIntegrityError(
                "authoring evidence disagrees with inference authority"
            )

        evidence_sha256 = coding_authoring_evidence_digest(authoring.evidence)
        self._validate_freeze(
            ticket,
            authoring_lease=authoring_lease,
            freeze=freeze,
            authoring_evidence_sha256=evidence_sha256,
        )
        self._require_active(ticket, phase="grading")
        grading_lease = await self._platform.request_coding_grading_lease(
            agent_id=ticket.agent_id,
            run_row_id=ticket.run_row_id,
            ticket_id=ticket.ticket_id,
            freeze_id=freeze.freeze_id,
            claim_instance_id=ticket.claim_instance_id,
            authoring_evidence_sha256=evidence_sha256,
            expected_frozen_patch_sha256=authoring.evidence.frozen_patch_sha256,
        )
        self._validate_grading_lease(
            ticket,
            authoring_lease=authoring_lease,
            grading_lease=grading_lease,
            freeze=freeze,
            authoring=authoring,
            authoring_evidence_sha256=evidence_sha256,
        )
        try:
            grading = await self._runtime.grade(grading_lease, authoring)
        except BaseException:
            await self._abort_grading(grading_lease)
            raise
        try:
            evidence = build_coding_run_evidence(
                grading_lease.run_manifest,
                str(ticket.ticket_id),
                grading.task_evidence,
            )
        except CodingTerminalEvidenceError as error:
            raise CodingAttemptIntegrityError(
                "coding grading evidence disagrees with run authority"
            ) from error

        return await self._platform.submit_coding_shadow_result(
            ticket.agent_id,
            bench_version=ticket.bench_version,
            run_row_id=ticket.run_row_id,
            ticket_id=ticket.ticket_id,
            ticket_deadline=ticket.ticket_deadline,
            agent_artifact_sha256=ticket.agent_artifact_sha256,
            screened_image_sha256=ticket.screened_image_sha256,
            run_manifest=grading_lease.run_manifest,
            evidence=evidence,
            task_evidence=list(grading.task_evidence),
        )

    async def submit_authoring_infrastructure_failure(
        self,
        ticket: CodingAttemptTicket,
        *,
        authoring_lease: CodingAuthoringLeaseResponse,
    ) -> SubmitCodingShadowResultResponse:
        """Publish a zero-score terminal when recovery cannot rerun authoring."""

        self._validate_authoring_lease(ticket, authoring_lease)
        tasks = tuple(
            build_coding_failure_task_evidence(
                authoring_lease.run_manifest,
                validator_ticket_id=str(ticket.ticket_id),
                case_id=task.case_id,
                variant_id=task.variant_id,
                stage=CodingFailureStage.AUTHORING_INFRASTRUCTURE,
                failure_code=CodingFailureCode.AUTHORING_RUNTIME,
            )
            for task in authoring_lease.run_manifest.tasks
        )
        try:
            evidence = build_coding_run_evidence(
                authoring_lease.run_manifest,
                str(ticket.ticket_id),
                tasks,
            )
        except CodingTerminalEvidenceError as error:
            raise CodingAttemptIntegrityError(
                "coding failure evidence disagrees with run authority"
            ) from error
        return await self._platform.submit_coding_shadow_result(
            ticket.agent_id,
            bench_version=ticket.bench_version,
            run_row_id=ticket.run_row_id,
            ticket_id=ticket.ticket_id,
            ticket_deadline=ticket.ticket_deadline,
            agent_artifact_sha256=ticket.agent_artifact_sha256,
            screened_image_sha256=ticket.screened_image_sha256,
            run_manifest=authoring_lease.run_manifest,
            evidence=evidence,
            task_evidence=list(tasks),
        )

    def _require_active(self, ticket: CodingAttemptTicket, *, phase: str) -> None:
        now = self._clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise CodingAttemptIntegrityError("coding attempt clock is not UTC-aware")
        if now.astimezone(UTC) >= ticket.ticket_deadline.astimezone(UTC):
            raise CodingAttemptExpiredError(f"coding ticket expired before {phase}")

    async def _abort_authoring(self, lease: CodingAuthoringLeaseResponse) -> None:
        try:
            await asyncio.shield(self._runtime.abort_authoring(lease))
        except BaseException as error:
            raise CodingAttemptIntegrityError(
                "coding authoring cleanup failed"
            ) from error

    async def _abort_grading(self, lease: CodingGradingLeaseResponse) -> None:
        try:
            await asyncio.shield(self._runtime.abort_grading(lease))
        except BaseException as error:
            raise CodingAttemptIntegrityError(
                "coding grading cleanup failed"
            ) from error

    @staticmethod
    def _validate_authoring_lease(
        ticket: CodingAttemptTicket,
        lease: CodingAuthoringLeaseResponse,
    ) -> None:
        manifest = lease.run_manifest
        if (
            lease.ticket_id != ticket.ticket_id
            or lease.ticket_deadline != ticket.ticket_deadline
            or manifest.agent_id != str(ticket.agent_id)
            or manifest.agent_artifact_sha256 != ticket.agent_artifact_sha256
            or len(manifest.tasks) != 1
            or lease.weight_eligible is not False
        ):
            raise CodingAttemptIntegrityError(
                "coding authoring lease disagrees with ticket authority"
            )

    @staticmethod
    def _validate_harness_launch(
        ticket: CodingAttemptTicket,
        lease: CodingAuthoringLeaseResponse,
        harness: CodingHarnessLaunchResponse,
    ) -> None:
        if (
            harness.agent_id != ticket.agent_id
            or harness.run_row_id != ticket.run_row_id
            or harness.ticket_id != ticket.ticket_id
            or harness.ticket_deadline != ticket.ticket_deadline
            or harness.bench_version != ticket.bench_version
            or harness.agent_artifact_sha256 != ticket.agent_artifact_sha256
            or harness.screened_image_sha256 != ticket.screened_image_sha256
            or harness.agent_artifact_sha256 != lease.run_manifest.agent_artifact_sha256
            or harness.weight_eligible is not False
        ):
            raise CodingAttemptIntegrityError(
                "coding harness launch disagrees with ticket authority"
            )

    @staticmethod
    def _validate_freeze(
        ticket: CodingAttemptTicket,
        *,
        authoring_lease: CodingAuthoringLeaseResponse,
        freeze: SubmitCodingAuthoringFreezeResponse,
        authoring_evidence_sha256: str,
    ) -> None:
        if (
            freeze.agent_id != ticket.agent_id
            or freeze.run_row_id != ticket.run_row_id
            or freeze.ticket_id != ticket.ticket_id
            or freeze.coding_run_id != authoring_lease.coding_run_id
            or freeze.authoring_evidence_sha256 != authoring_evidence_sha256
            or freeze.accepted is not True
            or freeze.weight_eligible is not False
        ):
            raise CodingAttemptIntegrityError(
                "coding authoring freeze response disagrees with authority"
            )

    @staticmethod
    def _validate_grading_lease(
        ticket: CodingAttemptTicket,
        *,
        authoring_lease: CodingAuthoringLeaseResponse,
        grading_lease: CodingGradingLeaseResponse,
        freeze: SubmitCodingAuthoringFreezeResponse,
        authoring: CodingAuthoringOutcome,
        authoring_evidence_sha256: str,
    ) -> None:
        if (
            grading_lease.agent_id != ticket.agent_id
            or grading_lease.run_row_id != ticket.run_row_id
            or grading_lease.ticket_id != ticket.ticket_id
            or grading_lease.ticket_deadline != ticket.ticket_deadline
            or grading_lease.freeze_id != freeze.freeze_id
            or grading_lease.authoring_evidence_sha256 != authoring_evidence_sha256
            or grading_lease.frozen_patch_sha256
            != authoring.evidence.frozen_patch_sha256
            or grading_lease.frozen_submission_object_key
            != authoring.frozen_submission_object_key
            or grading_lease.run_manifest != authoring_lease.run_manifest
            or grading_lease.run_manifest_sha256
            != canonical_digest(authoring_lease.run_manifest)
            or grading_lease.weight_eligible is not False
        ):
            raise CodingAttemptIntegrityError(
                "coding grading lease disagrees with frozen authority"
            )
