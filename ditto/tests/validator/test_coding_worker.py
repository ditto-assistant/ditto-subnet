from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal
from uuid import UUID

import pytest

from ditto.api_models.coding import SubmitCodingShadowResultResponse
from ditto.api_models.coding_claims import CodingClaimResponse
from ditto.api_models.coding_evidence_upload import (
    CodingSealedEvidenceFinalization,
    CodingSealedEvidenceKind,
    CodingSealedEvidenceUploadCapability,
)
from ditto.validator.coding_attempt import CodingAttemptIntegrityError
from ditto.validator.coding_publication import (
    PendingRelease,
    PreparedCodingPublication,
    PublicationArtifact,
    PublicationAuthority,
    PublicationRecord,
    ReleaseReservation,
    SealedEvidenceArtifact,
    SealedEvidenceManifest,
)
from ditto.validator.coding_supervisor import CodingSupervisorRecovery
from ditto.validator.coding_worker import (
    CodingShadowWorker,
    DurableCodingAttemptPlatform,
)

_NOW = datetime(2026, 8, 23, 22, tzinfo=UTC)
_TICKET = UUID("33333333-3333-4333-8333-333333333333")
_AGENT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_RUN = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_INSTANCE = "coding-shadow-worker-test"
_BODY = b'{"terminal":"signed"}'
_ACK = (
    b'{"agent_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",'
    b'"run_row_id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",'
    b'"ticket_id":"33333333-3333-4333-8333-333333333333",'
    b'"coding_run_id":"coding-run-001","accepted":true,'
    b'"idempotent":true,"weight_eligible":false}'
)
_INFERENCE_POLICY = "b2f38d9f6b5484e9a056d74be4dc0250912f05c9e51512801b590dff934a41d6"
_UPLOAD = UUID("44444444-4444-4444-8444-444444444444")


def _claim(*, started: bool) -> CodingClaimResponse:
    return CodingClaimResponse(
        schema="dittobench-coding-ticket-claim-v1",
        coding_contract_version=1,
        weight_eligible=False,
        validator_hotkey="5" + "V" * 47,
        instance_id=_INSTANCE,
        claim_generation=1,
        claim_expires_at=_NOW + timedelta(minutes=2),
        claim_started_at=_NOW if started else None,
        idempotent=False,
        agent_id=_AGENT,
        run_row_id=_RUN,
        ticket_id=_TICKET,
        ticket_deadline=_NOW + timedelta(hours=1),
        bench_version=12,
        coding_run_id="coding-run-001",
        agent_artifact_sha256="aa" * 32,
        screened_image_sha256="bb" * 32,
        run_manifest_sha256="cc" * 32,
        task_set_manifest_sha256="dd" * 32,
    )


def _capability(
    kind: CodingSealedEvidenceKind,
    sha256: str,
    size_bytes: int,
    claim: CodingClaimResponse,
) -> CodingSealedEvidenceUploadCapability:
    expires_at = min(_NOW + timedelta(minutes=1), claim.claim_expires_at)
    return CodingSealedEvidenceUploadCapability(
        schema="dittobench-coding-sealed-evidence-upload-capability-v1",
        coding_contract_version=1,
        weight_eligible=False,
        ticket_id=claim.ticket_id,
        claim_generation=claim.claim_generation,
        ticket_deadline=claim.ticket_deadline,
        upload_id=_UPLOAD,
        evidence_kind=kind,
        sha256=sha256,
        size_bytes=size_bytes,
        content_type="application/octet-stream",
        checksum_sha256_b64=base64.b64encode(bytes.fromhex(sha256)).decode(),
        url=(
            f"https://storage.test/coding-evidence/v1/{kind.value}/sha256/{sha256}"
            "?X-Amz-Date=20260823T220000Z&X-Amz-Expires=60"
            "&X-Amz-Signature=synthetic"
        ),
        expires_at=expires_at,
    )


def _finalization(
    kind: CodingSealedEvidenceKind,
    sha256: str,
    size_bytes: int,
    claim: CodingClaimResponse,
) -> CodingSealedEvidenceFinalization:
    return CodingSealedEvidenceFinalization(
        schema="dittobench-coding-sealed-evidence-finalized-v1",
        coding_contract_version=1,
        weight_eligible=False,
        ticket_id=claim.ticket_id,
        claim_generation=claim.claim_generation,
        upload_id=_UPLOAD,
        evidence_kind=kind,
        sha256=sha256,
        size_bytes=size_bytes,
        finalized_at=_NOW,
        accepted=True,
        idempotent=False,
    )


class _Runtime:
    def __init__(
        self,
        state: Literal["ambiguous", "terminal_pending", "released"] = "ambiguous",
    ) -> None:
        self.state = state
        self.recoveries = 0

    async def recover(self, **_: Any) -> CodingSupervisorRecovery:
        self.recoveries += 1
        if self.state == "terminal_pending" and self.recoveries == 1:
            return CodingSupervisorRecovery(
                state="terminal_pending",
                publication_stage="terminal_result",
                request_sha256=hashlib.sha256(_BODY).hexdigest(),
            )
        state = "released" if self.state == "terminal_pending" else self.state
        return CodingSupervisorRecovery(
            state=state,
            publication_stage=None,
            request_sha256=None,
        )


class _Publication:
    def __init__(self) -> None:
        request_sha = hashlib.sha256(_BODY).hexdigest()
        self.record = PublicationRecord(
            record_id="11" * 32,
            ticket_id=_TICKET,
            stage="terminal_result",
            authority=PublicationAuthority(
                agent_id=_AGENT,
                bench_version=12,
                run_row_id=_RUN,
                coding_run_id="coding-run-001",
                screened_image_sha256="bb" * 32,
                run_manifest_sha256="cc" * 32,
                task_set_manifest_sha256="dd" * 32,
                evidence_sha256="ee" * 32,
            ),
            request=PublicationArtifact(
                object_key="sha256/" + request_sha,
                sha256=request_sha,
                size_bytes=len(_BODY),
            ),
            acknowledgement=None,
        )
        self.acknowledged = 0
        self.preflights = 0
        self.release_preparations = 0
        self.released = 0
        self.pending_release: PendingRelease | None = None

    async def pending(self, **_: Any) -> list[Any]:
        self.preflights += 1
        return []

    async def pending_releases(self, **_: Any) -> list[PendingRelease]:
        return [self.pending_release] if self.pending_release is not None else []

    async def lookup(self, **_: Any) -> PublicationRecord:
        return self.record

    async def open(self, **authority: Any) -> bytes:
        return _ACK if authority.get("acknowledgement") else _BODY

    async def prepare(self, **_: Any) -> tuple[str, PublicationArtifact]:
        return self.record.record_id, self.record.request

    async def acknowledge(self, **_: Any) -> PublicationArtifact:
        self.acknowledged += 1
        digest = hashlib.sha256(_ACK).hexdigest()
        artifact = PublicationArtifact(
            object_key="sha256/" + digest,
            sha256=digest,
            size_bytes=len(_ACK),
        )
        self.record = self.record.model_copy(update={"acknowledgement": artifact})
        return artifact

    async def evidence_manifest(self, **_: Any) -> SealedEvidenceManifest:
        evidence = [
            SealedEvidenceArtifact(
                evidence_kind="authoring-transcript",
                sha256="aa" * 32,
                size_bytes=1024,
            ),
            SealedEvidenceArtifact(
                evidence_kind="terminal-publication-request",
                sha256=self.record.request.sha256,
                size_bytes=self.record.request.size_bytes,
            ),
        ]
        if self.record.acknowledgement is not None:
            evidence.append(
                SealedEvidenceArtifact(
                    evidence_kind="terminal-publication-acknowledgement",
                    sha256=self.record.acknowledgement.sha256,
                    size_bytes=self.record.acknowledgement.size_bytes,
                )
            )
        return SealedEvidenceManifest(
            schema="dittobench-coding-sealed-evidence-manifest-v1",
            coding_contract_version=1,
            weight_eligible=False,
            ticket_id=_TICKET,
            record_id=self.record.record_id,
            evidence=evidence,
        )

    async def prepare_release(
        self,
        *,
        record_id: str,
        terminal_evidence_sha256: str,
        capability: CodingSealedEvidenceUploadCapability,
    ) -> ReleaseReservation:
        assert record_id == self.record.record_id
        assert terminal_evidence_sha256 == self.record.authority.evidence_sha256
        reservation = ReleaseReservation(
            ticket_id=capability.ticket_id,
            claim_generation=capability.claim_generation,
            upload_id=capability.upload_id,
            evidence_kind="terminal-publication-acknowledgement",
            sha256=capability.sha256,
            size_bytes=capability.size_bytes,
        )
        pending = PendingRelease(
            record_id=record_id,
            ticket_id=_TICKET,
            terminal_evidence_sha256=terminal_evidence_sha256,
            reservation=reservation,
        )
        if self.pending_release is not None:
            assert self.pending_release == pending
        self.pending_release = pending
        self.release_preparations += 1
        return reservation

    async def release(self, **authority: Any) -> None:
        assert authority["record_id"] == self.record.record_id
        self.pending_release = None
        self.released += 1


class _Uploader:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.claims: list[CodingClaimResponse] = []

    async def upload(
        self,
        claim: CodingClaimResponse,
        *,
        record_id: str,
        evidence_kind: CodingSealedEvidenceKind,
        sha256: str,
        size_bytes: int,
    ) -> CodingSealedEvidenceFinalization:
        assert record_id == "11" * 32 and len(sha256) == 64 and size_bytes > 0
        self.events.append(f"upload:{evidence_kind.value}")
        self.claims.append(claim)
        return _finalization(evidence_kind, sha256, size_bytes, claim)

    async def reserve(
        self,
        claim: CodingClaimResponse,
        *,
        evidence_kind: CodingSealedEvidenceKind,
        sha256: str,
        size_bytes: int,
    ) -> CodingSealedEvidenceUploadCapability:
        self.events.append(f"reserve:{evidence_kind.value}")
        self.claims.append(claim)
        return _capability(evidence_kind, sha256, size_bytes, claim)

    async def upload_reserved(
        self,
        claim: CodingClaimResponse,
        *,
        record_id: str,
        capability: CodingSealedEvidenceUploadCapability,
    ) -> CodingSealedEvidenceFinalization:
        assert record_id == "11" * 32
        self.events.append(f"upload_reserved:{capability.evidence_kind.value}")
        self.claims.append(claim)
        return _finalization(
            capability.evidence_kind,
            capability.sha256,
            capability.size_bytes,
            claim,
        )


class _AuthoringPublication(_Publication):
    def __init__(self) -> None:
        super().__init__()
        self.record = self.record.model_copy(update={"stage": "authoring_freeze"})

    async def evidence_manifest(self, **_: Any) -> SealedEvidenceManifest:
        evidence = [
            SealedEvidenceArtifact(
                evidence_kind="authoring-transcript",
                sha256="aa" * 32,
                size_bytes=1024,
            ),
            SealedEvidenceArtifact(
                evidence_kind="frozen-submission",
                sha256="bb" * 32,
                size_bytes=2048,
            ),
            SealedEvidenceArtifact(
                evidence_kind="authoring-publication-request",
                sha256=self.record.request.sha256,
                size_bytes=self.record.request.size_bytes,
            ),
        ]
        if self.record.acknowledgement is not None:
            evidence.append(
                SealedEvidenceArtifact(
                    evidence_kind="authoring-publication-acknowledgement",
                    sha256=self.record.acknowledgement.sha256,
                    size_bytes=self.record.acknowledgement.size_bytes,
                )
            )
        return SealedEvidenceManifest(
            schema="dittobench-coding-sealed-evidence-manifest-v1",
            coding_contract_version=1,
            weight_eligible=False,
            ticket_id=_TICKET,
            record_id=self.record.record_id,
            evidence=evidence,
        )


class _Platform:
    def __init__(self, claim: CodingClaimResponse) -> None:
        self.claim = claim
        self.events: list[str] = []
        self.replay_result: CodingSealedEvidenceFinalization | None = None
        self.heartbeat_event = asyncio.Event()

    async def claim_next_coding_ticket(self, instance_id: str) -> CodingClaimResponse:
        assert instance_id == _INSTANCE
        self.events.append("claim")
        return self.claim

    async def start_coding_ticket_claim(
        self, claim: CodingClaimResponse
    ) -> CodingClaimResponse:
        self.events.append("start")
        return claim.model_copy(update={"claim_started_at": _NOW})

    async def heartbeat_coding_ticket_claim(
        self, claim: CodingClaimResponse
    ) -> CodingClaimResponse:
        self.events.append("heartbeat")
        renewed = claim.model_copy(
            update={"claim_expires_at": _NOW + timedelta(minutes=5)}
        )
        self.heartbeat_event.set()
        return renewed

    async def replay_coding_evidence_finalization(
        self,
        pending: PendingRelease,
        *,
        instance_id: str,
    ) -> CodingSealedEvidenceFinalization | None:
        assert pending.ticket_id == _TICKET and instance_id == _INSTANCE
        self.events.append("replay")
        return self.replay_result

    async def request_coding_authoring_lease(self, ticket_id: UUID) -> Any:
        assert ticket_id == _TICKET
        self.events.append("authoring_lease")
        return SimpleNamespace(
            ticket_id=ticket_id,
            ticket_deadline=_NOW + timedelta(hours=1),
            run_manifest=SimpleNamespace(
                inference_grant_sha256=_INFERENCE_POLICY,
                tasks=[
                    SimpleNamespace(
                        case_id="case-001",
                        profile_capability_id="profile-001",
                    )
                ],
            ),
            budgets=SimpleNamespace(
                workspace_tool_calls=150,
                model_input_tokens=200_000,
                model_output_tokens=30_000,
            ),
            capabilities=[SimpleNamespace(expires_at=_NOW + timedelta(minutes=5))],
        )

    async def request_coding_harness_launch(self, ticket_id: UUID) -> Any:
        assert ticket_id == _TICKET
        self.events.append("harness")
        return SimpleNamespace(
            ticket_id=ticket_id,
            expires_at=_NOW + timedelta(minutes=5),
        )

    async def request_coding_inference_grant(self, ticket_id: UUID) -> Any:
        assert ticket_id == _TICKET
        self.events.append("grant")
        return SimpleNamespace(
            ticket_id=ticket_id,
            inference_grant_sha256=_INFERENCE_POLICY,
            case_id="case-001",
            profile_capability_id="profile-001",
            expires_at=_NOW + timedelta(minutes=30),
            request_budget=166,
            prompt_token_budget=200_000,
            completion_token_budget=30_000,
        )

    async def publish_prepared_coding_publication(
        self, prepared: Any
    ) -> tuple[SubmitCodingShadowResultResponse, bytes]:
        assert prepared.body == _BODY
        self.events.append("publish")
        return (
            SubmitCodingShadowResultResponse(
                agent_id=_AGENT,
                run_row_id=_RUN,
                ticket_id=_TICKET,
                coding_run_id="coding-run-001",
                accepted=True,
                idempotent=True,
                weight_eligible=False,
            ),
            _ACK,
        )


async def test_authoring_publication_finalizes_phase_manifest_in_order() -> None:
    claim = _claim(started=True)
    platform = _Platform(claim)
    publication = _AuthoringPublication()
    uploader = _Uploader()

    async def current_claim() -> CodingClaimResponse:
        return claim

    durable = DurableCodingAttemptPlatform(
        platform,
        publication,  # type: ignore[arg-type]
        uploader,  # type: ignore[arg-type]
        current_claim,
        lambda: None,
    )
    await durable.publish(
        PreparedCodingPublication(
            stage="authoring_freeze",
            ticket_id=_TICKET,
            agent_id=_AGENT,
            authority=publication.record.authority,
            body=_BODY,
        )
    )
    assert platform.events == ["publish"]
    assert publication.acknowledged == 1
    assert uploader.events == [
        "upload:authoring-transcript",
        "upload:frozen-submission",
        "upload:authoring-publication-request",
        "upload:authoring-publication-acknowledgement",
    ]


async def test_publication_rejects_incomplete_manifest_before_platform() -> None:
    claim = _claim(started=True)
    platform = _Platform(claim)
    publication = _AuthoringPublication()
    uploader = _Uploader()
    complete_manifest = await publication.evidence_manifest()

    async def incomplete_manifest(**_: Any) -> SealedEvidenceManifest:
        return complete_manifest.model_copy(
            update={"evidence": complete_manifest.evidence[:-1]}
        )

    publication.evidence_manifest = incomplete_manifest  # type: ignore[method-assign]

    async def current_claim() -> CodingClaimResponse:
        return claim

    durable = DurableCodingAttemptPlatform(
        platform,
        publication,  # type: ignore[arg-type]
        uploader,  # type: ignore[arg-type]
        current_claim,
        lambda: None,
    )
    with pytest.raises(CodingAttemptIntegrityError, match="manifest is incomplete"):
        await durable.publish(
            PreparedCodingPublication(
                stage="authoring_freeze",
                ticket_id=_TICKET,
                agent_id=_AGENT,
                authority=publication.record.authority,
                body=_BODY,
            )
        )
    assert platform.events == []
    assert uploader.events == []


async def test_new_claim_starts_before_coordinator_and_executes_once() -> None:
    platform = _Platform(_claim(started=False))
    publication = _Publication()
    worker = CodingShadowWorker(
        platform=platform,
        runtime=_Runtime("released"),  # type: ignore[arg-type]
        publication=publication,  # type: ignore[arg-type]
        uploader=_Uploader(),  # type: ignore[arg-type]
        instance_id=_INSTANCE,
        clock=lambda: _NOW,
    )

    async def execute_prepared(ticket: Any, **_: Any) -> object:
        assert ticket.ticket_id == _TICKET
        platform.events.append("execute")
        return object()

    worker._coordinator = SimpleNamespace(  # type: ignore[assignment]
        execute_prepared=execute_prepared,
        validate_preflight=lambda *_args, **_kwargs: None,
    )
    assert await worker.run_once() is True
    assert platform.events == [
        "claim",
        "authoring_lease",
        "harness",
        "grant",
        "start",
        "execute",
    ]
    assert publication.preflights == 1


async def test_started_ambiguous_claim_never_reruns_candidate() -> None:
    platform = _Platform(_claim(started=True))
    runtime = _Runtime("ambiguous")
    worker = CodingShadowWorker(
        platform=platform,
        runtime=runtime,  # type: ignore[arg-type]
        publication=_Publication(),  # type: ignore[arg-type]
        uploader=_Uploader(),  # type: ignore[arg-type]
        instance_id=_INSTANCE,
        clock=lambda: _NOW,
    )
    worker._coordinator = SimpleNamespace(  # type: ignore[assignment]
        execute=lambda _: (_ for _ in ()).throw(AssertionError("candidate rerun"))
    )
    assert await worker.run_once() is False
    assert runtime.recoveries == 1
    assert platform.events == ["claim"]


async def test_preflight_failure_leaves_claim_unstarted_and_transferable() -> None:
    platform = _Platform(_claim(started=False))

    async def mismatched_grant(ticket_id: UUID) -> Any:
        assert ticket_id == _TICKET
        platform.events.append("grant")
        return SimpleNamespace(
            ticket_id=ticket_id,
            inference_grant_sha256="ff" * 32,
            case_id="case-001",
            profile_capability_id="profile-001",
            expires_at=_NOW + timedelta(minutes=30),
            request_budget=166,
            prompt_token_budget=200_000,
            completion_token_budget=30_000,
        )

    platform.request_coding_inference_grant = mismatched_grant  # type: ignore[method-assign]
    worker = CodingShadowWorker(
        platform=platform,
        runtime=_Runtime("released"),  # type: ignore[arg-type]
        publication=_Publication(),  # type: ignore[arg-type]
        uploader=_Uploader(),  # type: ignore[arg-type]
        instance_id=_INSTANCE,
        clock=lambda: _NOW,
    )
    worker._coordinator = SimpleNamespace(  # type: ignore[assignment]
        validate_preflight=lambda *_args, **_kwargs: None
    )
    with pytest.raises(CodingAttemptIntegrityError, match="authority"):
        await worker.run_once()
    assert "start" not in platform.events


async def test_started_terminal_pending_replays_exact_bytes_only() -> None:
    platform = _Platform(_claim(started=True))
    publication = _Publication()
    uploader = _Uploader()
    worker = CodingShadowWorker(
        platform=platform,
        runtime=_Runtime("terminal_pending"),  # type: ignore[arg-type]
        publication=publication,  # type: ignore[arg-type]
        uploader=uploader,  # type: ignore[arg-type]
        instance_id=_INSTANCE,
        clock=lambda: _NOW,
    )
    assert await worker.run_once() is True
    assert platform.events == ["claim", "publish"]
    assert publication.acknowledged == 1
    assert publication.preflights == 1
    assert publication.release_preparations == 1
    assert publication.released == 1
    assert uploader.events == [
        "upload:authoring-transcript",
        "upload:terminal-publication-request",
        "reserve:terminal-publication-acknowledgement",
        "upload_reserved:terminal-publication-acknowledgement",
    ]


async def test_pending_release_replays_before_claiming_new_work() -> None:
    claim = _claim(started=True)
    platform = _Platform(claim)
    publication = _Publication()
    acknowledgement = await publication.acknowledge()
    capability = _capability(
        CodingSealedEvidenceKind.TERMINAL_PUBLICATION_ACKNOWLEDGEMENT,
        acknowledgement.sha256,
        acknowledgement.size_bytes,
        claim,
    )
    await publication.prepare_release(
        record_id=publication.record.record_id,
        terminal_evidence_sha256=publication.record.authority.evidence_sha256,
        capability=capability,
    )
    platform.replay_result = _finalization(
        capability.evidence_kind,
        capability.sha256,
        capability.size_bytes,
        claim,
    ).model_copy(update={"idempotent": True})
    uploader = _Uploader()
    worker = CodingShadowWorker(
        platform=platform,
        runtime=_Runtime("terminal_pending"),  # type: ignore[arg-type]
        publication=publication,  # type: ignore[arg-type]
        uploader=uploader,  # type: ignore[arg-type]
        instance_id=_INSTANCE,
        clock=lambda: _NOW,
    )
    assert await worker.run_once() is True
    assert platform.events == ["replay"]
    assert publication.released == 1
    assert publication.pending_release is None
    assert uploader.events == []


async def test_unfinalized_pending_release_resumes_under_same_claim() -> None:
    claim = _claim(started=True)
    platform = _Platform(claim)
    publication = _Publication()
    acknowledgement = await publication.acknowledge()
    capability = _capability(
        CodingSealedEvidenceKind.TERMINAL_PUBLICATION_ACKNOWLEDGEMENT,
        acknowledgement.sha256,
        acknowledgement.size_bytes,
        claim,
    )
    await publication.prepare_release(
        record_id=publication.record.record_id,
        terminal_evidence_sha256=publication.record.authority.evidence_sha256,
        capability=capability,
    )
    uploader = _Uploader()
    worker = CodingShadowWorker(
        platform=platform,
        runtime=_Runtime("terminal_pending"),  # type: ignore[arg-type]
        publication=publication,  # type: ignore[arg-type]
        uploader=uploader,  # type: ignore[arg-type]
        instance_id=_INSTANCE,
        clock=lambda: _NOW,
    )
    assert await worker.run_once() is True
    assert platform.events == ["replay", "claim"]
    assert publication.released == 1
    assert uploader.events == [
        "reserve:terminal-publication-acknowledgement",
        "upload_reserved:terminal-publication-acknowledgement",
    ]


async def test_heartbeat_updates_claim_used_by_publication() -> None:
    claim = _claim(started=True).model_copy(
        update={"claim_expires_at": _NOW + timedelta(seconds=3)}
    )
    platform = _Platform(claim)
    worker = CodingShadowWorker(
        platform=platform,
        runtime=_Runtime("released"),  # type: ignore[arg-type]
        publication=_Publication(),  # type: ignore[arg-type]
        uploader=_Uploader(),  # type: ignore[arg-type]
        instance_id=_INSTANCE,
        clock=lambda: _NOW,
    )

    async def observe_renewal() -> datetime:
        await platform.heartbeat_event.wait()
        current = await worker._current_claim()
        return current.claim_expires_at

    expires_at = await worker._with_heartbeat(claim, observe_renewal)
    assert expires_at == _NOW + timedelta(minutes=5)
    assert platform.events == ["heartbeat"]


async def test_terminal_finalization_stops_rejected_heartbeat() -> None:
    claim = _claim(started=True).model_copy(
        update={"claim_expires_at": _NOW + timedelta(seconds=3)}
    )
    platform = _Platform(claim)

    async def rejected_heartbeat(_: CodingClaimResponse) -> CodingClaimResponse:
        platform.events.append("heartbeat_rejected")
        raise RuntimeError("terminal finalization closed claim")

    platform.heartbeat_coding_ticket_claim = rejected_heartbeat  # type: ignore[assignment]
    worker = CodingShadowWorker(
        platform=platform,
        runtime=_Runtime("released"),  # type: ignore[arg-type]
        publication=_Publication(),  # type: ignore[arg-type]
        uploader=_Uploader(),  # type: ignore[arg-type]
        instance_id=_INSTANCE,
        clock=lambda: _NOW,
    )

    async def finalize_then_finish() -> str:
        await asyncio.sleep(0.8)
        worker._mark_terminal_finalized()
        await asyncio.sleep(0.3)
        return "released"

    assert await worker._with_heartbeat(claim, finalize_then_finish) == "released"
    assert platform.events == ["heartbeat_rejected"]
