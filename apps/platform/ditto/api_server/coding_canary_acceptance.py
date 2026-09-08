"""Read-only native canary evidence checks; no launch, repair or rollout approval."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_hosted import HostedCodingResult, hosted_message_digest
from ditto.api_models.coding_hosted_control import AuthoringIdentity
from ditto.api_models.coding_hosted_grading import HostedTerminalIdentity
from ditto.api_server.coding_evidence_recovery import (
    HostedEvidenceRecovery,
    RecoveryTarget,
)
from ditto.api_server.coding_hosted_authoring_evidence import canonical, sha
from ditto.api_server.coding_hosted_evidence_spool import HostedEvidenceError
from ditto.api_server.coding_hosted_verification import (
    HostedResultExpectation,
    SignatureVerifier,
    verify_hosted_result,
)
from ditto.db.models import (
    CodingHostedAssignment,
    CodingHostedEvidenceReservation,
    CodingHostedInferenceGrant,
    CodingHostedInferenceRequest,
    CodingHostedPrivateTask,
    CodingHostedResultAcknowledgement,
    CodingHostedResultDelivery,
)

Phase = Literal["inference", "authoring", "terminal"]


class CanaryEvidenceVerificationError(HostedEvidenceError):
    def __init__(self, stage: str):
        allowed = {
            "target",
            "result_binding",
            "ledger",
            "final_ledger",
            "readback_inference",
            "readback_authoring",
            "readback_terminal",
        }
        self.stage = stage if stage in allowed else "internal"
        super().__init__("native canary evidence verification failed")


@dataclass(frozen=True, repr=False)
class CanaryEvidenceTarget:
    expected: HostedResultExpectation
    worker_id: UUID
    result_sha256: str
    terminal_sha256: str

    def check(self) -> None:
        if not isinstance(self.worker_id, UUID) or not self.worker_id.int:
            raise HostedEvidenceError("canary worker identity invalid")
        for value in (self.result_sha256, self.terminal_sha256):
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{64}", value) is None
            ):
                raise HostedEvidenceError("canary evidence identity invalid")


class CanaryEvidenceVerifier:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        readbacks: Mapping[Phase, HostedEvidenceRecovery],
        trusted_verifiers: Mapping[str, SignatureVerifier],
    ):
        if set(readbacks) != {"inference", "authoring", "terminal"}:
            raise HostedEvidenceError("canary readback phases incomplete")
        self._sessions, self._readbacks, self._verifiers = (
            sessions,
            readbacks,
            trusted_verifiers,
        )

    def _signed(self, body: bytes, target: CanaryEvidenceTarget) -> HostedCodingResult:
        result = verify_hosted_result(
            body=body,
            expected=target.expected,
            trusted_verifiers=self._verifiers,
            now_unix=int(time.time()),
        )
        if (
            hosted_message_digest(result) != target.result_sha256
            or result.evidence_sha256 != target.terminal_sha256
            or result.outcome != "completed"
        ):
            raise HostedEvidenceError("canary signed outcome differs")
        return result

    async def _snapshot(
        self, result: HostedCodingResult, target: CanaryEvidenceTarget
    ) -> list[RecoveryTarget]:
        def recovery(
            phase: Phase, digest: str, request: UUID | None = None
        ) -> RecoveryTarget:
            return RecoveryTarget(
                phase,
                target.worker_id,
                result.evaluation_id,
                result.attempt_id,
                digest,
                request,
            )

        async with asyncio.timeout(30), self._sessions() as session, session.begin():
            # A coherent observation and a database-enforced no-write boundary.
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            assignment = await session.get(CodingHostedAssignment, result.evaluation_id)
            task = await session.get(CodingHostedPrivateTask, result.evaluation_id)
            delivery = await session.get(
                CodingHostedResultDelivery, target.result_sha256
            )
            acknowledgement = await session.get(
                CodingHostedResultAcknowledgement, target.result_sha256
            )
            if (
                assignment is None
                or task is None
                or delivery is None
                or acknowledgement is None
                or assignment.admitted_at is None
                or assignment.started_at is None
                or assignment.worker_id != target.worker_id
                or assignment.attempt_id != result.attempt_id
                or assignment.assignment_sha256 != result.assignment_sha256
                or assignment.validator_hotkey != result.validator_hotkey
                or assignment.artifact_sha256 != result.artifact_sha256
                or assignment.authority.get("policy_sha256") != result.policy_sha256
                or assignment.authority.get("execution_profile_sha256")
                != result.execution_profile_sha256
                or assignment.authority.get("grading_profile_sha256")
                != result.grading_profile_sha256
                or delivery.evaluation_id != result.evaluation_id
                or delivery.validator_hotkey != result.validator_hotkey
                or acknowledgement.acknowledged_at < delivery.created_at
                or task.closed_at is None
                or task.close_reason != "completed"
                or task.frozen_at is None
            ):
                raise HostedEvidenceError("canary lifecycle incomplete or mismatched")
            # Another valid SR25519 signature for identical signing bytes is allowed.
            self._signed(canonical(delivery.body, 8192), target)
            terminal_target = recovery("terminal", target.terminal_sha256)
            terminal = await self._readbacks["terminal"]._record(
                session, terminal_target, lock=False
            )
            if not terminal.finalized or not isinstance(
                terminal.identity, HostedTerminalIdentity
            ):
                raise HostedEvidenceError("canary terminal is not finalized")
            authoring_target = recovery(
                "authoring", terminal.identity.authoring_evidence_sha256
            )
            authoring = await self._readbacks["authoring"]._record(
                session, authoring_target, lock=False
            )
            if (
                not authoring.finalized
                or not isinstance(authoring.identity, AuthoringIdentity)
                or authoring.identity.source != terminal.identity.source
                or terminal.identity.outcome != "completed"
                or not authoring.identity.header.completed_run
                or authoring.identity.patch_sha256 != task.frozen_patch_sha256
                or authoring.identity.patch_size != task.frozen_patch_size
            ):
                raise HostedEvidenceError("canary frozen authoring evidence differs")
            grant = await session.scalar(
                select(CodingHostedInferenceGrant).where(
                    CodingHostedInferenceGrant.evaluation_id == result.evaluation_id
                )
            )
            if (
                grant is None
                or grant.revoked_at is None
                or grant.worker_id != target.worker_id
                or grant.attempt_id != result.attempt_id
                or grant.assignment_sha256 != result.assignment_sha256
                or grant.policy_sha256 != result.policy_sha256
                or grant.execution_profile_sha256 != result.execution_profile_sha256
            ):
                raise HostedEvidenceError("canary inference grant is not closed")
            requests = (
                await session.scalars(
                    select(CodingHostedInferenceRequest)
                    .where(CodingHostedInferenceRequest.grant_id == grant.grant_id)
                    .order_by(CodingHostedInferenceRequest.sequence)
                    .limit(257)
                )
            ).all()
            # A full-path canary must exercise inference, not merely skip that path.
            if not 1 <= len(requests) <= 256:
                raise HostedEvidenceError(
                    "canary inference path not exercised or exceeds bound"
                )
            targets, digests = [], []
            for request in requests:
                reservation = await session.get(
                    CodingHostedEvidenceReservation, request.request_id
                )
                if reservation is None or request.state != "settled":
                    raise HostedEvidenceError("canary inference evidence incomplete")
                item = recovery(
                    "inference", reservation.identity_sha256, request.request_id
                )
                checked = await self._readbacks["inference"]._record(
                    session, item, lock=False
                )
                if not checked.finalized:
                    raise HostedEvidenceError("canary inference evidence not finalized")
                targets.append(item)
                digests.append(reservation.identity_sha256)
            linked = sha(
                canonical(
                    {
                        "schema": "dittobench-coding-hosted-inference-evidence-set-v2",
                        "grant_id": str(grant.grant_id),
                        "identities": digests,
                    },
                    32768,
                )
            )
            if linked != authoring.identity.inference_evidence_sha256:
                raise HostedEvidenceError("canary inference set differs from authoring")
            return [*targets, authoring_target, terminal_target]

    async def verify(self, body: bytes, target: CanaryEvidenceTarget) -> dict:
        stage = "target"
        try:
            target.check()
            async with asyncio.timeout(3600):
                stage = "result_binding"
                result = self._signed(body, target)
                stage = "ledger"
                before = await self._snapshot(result, target)
                receipts = []
                for item in before:
                    stage = "result_binding"
                    self._signed(
                        body, target
                    )  # expired responses never authorize more reads
                    stage = "readback_" + item.phase
                    receipts.append(
                        await self._readbacks[item.phase].verify_readback(item)
                    )
                stage = "final_ledger"
                if await self._snapshot(self._signed(body, target), target) != before:
                    raise HostedEvidenceError(
                        "canary lifecycle changed during readback"
                    )
                return {
                    "schema": "dittobench-coding-canary-evidence-verification-v2",
                    "evaluation_id": str(result.evaluation_id),
                    "attempt_id": str(result.attempt_id),
                    "worker_id": str(target.worker_id),
                    "assignment_sha256": result.assignment_sha256,
                    "result_sha256": target.result_sha256,
                    "terminal_sha256": target.terminal_sha256,
                    "inference_requests": len(before) - 2,
                    "readbacks": receipts,
                    "checked_at": datetime.now(UTC).isoformat(),
                    "canary_evidence_verified": True,
                    "reexecuted": False,
                    "pending_acceptance": [
                        "current_release_and_key_approval",
                        "native_host_qualification",
                        "native_matrix_acceptance",
                        "calibration",
                        "physical_cleanup",
                        "rollback_review",
                    ],
                    "rollout_approved": False,
                    "shadow_only": True,
                    "weight_eligible": False,
                }
        except Exception:
            raise CanaryEvidenceVerificationError(stage) from None
