from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal
from uuid import UUID

import pytest

from ditto.api_models.coding import SubmitCodingShadowResultResponse
from ditto.api_models.coding_claims import CodingClaimResponse
from ditto.validator.coding_attempt import CodingAttemptIntegrityError
from ditto.validator.coding_publication import (
    PublicationArtifact,
    PublicationAuthority,
    PublicationRecord,
)
from ditto.validator.coding_supervisor import CodingSupervisorRecovery
from ditto.validator.coding_worker import CodingShadowWorker

_NOW = datetime(2026, 8, 23, 22, tzinfo=UTC)
_TICKET = UUID("33333333-3333-4333-8333-333333333333")
_AGENT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_RUN = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_INSTANCE = "coding-shadow-worker-test"
_BODY = b'{"terminal":"signed"}'
_ACK = b'{"terminal":"accepted"}'
_INFERENCE_POLICY = "b2f38d9f6b5484e9a056d74be4dc0250912f05c9e51512801b590dff934a41d6"


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
        self.remote = False
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

    async def pending(self, **_: Any) -> list[Any]:
        self.preflights += 1
        return []

    async def lookup(self, **_: Any) -> PublicationRecord:
        return self.record

    async def open(self, **_: Any) -> bytes:
        return _BODY

    async def prepare(self, **_: Any) -> tuple[str, PublicationArtifact]:
        return self.record.record_id, self.record.request

    async def acknowledge(self, **_: Any) -> PublicationArtifact:
        self.acknowledged += 1
        digest = hashlib.sha256(_ACK).hexdigest()
        return PublicationArtifact(
            object_key="sha256/" + digest,
            sha256=digest,
            size_bytes=len(_ACK),
        )


class _Platform:
    def __init__(self, claim: CodingClaimResponse) -> None:
        self.claim = claim
        self.events: list[str] = []

    async def claim_next_coding_ticket(
        self, instance_id: str, run_row_id: UUID
    ) -> CodingClaimResponse:
        assert instance_id == _INSTANCE
        assert run_row_id == _RUN
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
        return claim

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


async def test_new_claim_starts_before_coordinator_and_executes_once() -> None:
    platform = _Platform(_claim(started=False))
    publication = _Publication()
    worker = CodingShadowWorker(
        platform=platform,
        runtime=_Runtime("released"),  # type: ignore[arg-type]
        publication=publication,  # type: ignore[arg-type]
        instance_id=_INSTANCE,
        run_row_id=_RUN,
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


async def test_remote_publication_preflight_uses_unstarted_claim_authority() -> None:
    platform = _Platform(_claim(started=False))
    publication = _Publication()
    publication.remote = True

    async def pending(**values: Any) -> list[Any]:
        authority = values["executor_authority"]
        assert authority.agent_id == _AGENT
        assert authority.agent_artifact_sha256 == "aa" * 32
        assert authority.coding_run_id == "coding-run-001"
        assert authority.ticket_id == _TICKET
        assert authority.deadline == _NOW + timedelta(hours=1)
        platform.events.append("publication_preflight")
        return []

    publication.pending = pending  # type: ignore[method-assign]
    worker = CodingShadowWorker(
        platform=platform,
        runtime=_Runtime("released"),  # type: ignore[arg-type]
        publication=publication,  # type: ignore[arg-type]
        instance_id=_INSTANCE,
        run_row_id=_RUN,
        clock=lambda: _NOW,
    )

    async def execute_prepared(ticket: Any, **_: Any) -> object:
        platform.events.append("execute")
        return ticket

    worker._coordinator = SimpleNamespace(  # type: ignore[assignment]
        execute_prepared=execute_prepared,
        validate_preflight=lambda *_args, **_kwargs: None,
    )
    assert await worker.run_once() is True
    assert platform.events == [
        "claim",
        "publication_preflight",
        "authoring_lease",
        "harness",
        "grant",
        "start",
        "execute",
    ]


async def test_started_ambiguous_claim_never_reruns_candidate() -> None:
    platform = _Platform(_claim(started=True))
    runtime = _Runtime("ambiguous")
    worker = CodingShadowWorker(
        platform=platform,
        runtime=runtime,  # type: ignore[arg-type]
        publication=_Publication(),  # type: ignore[arg-type]
        instance_id=_INSTANCE,
        run_row_id=_RUN,
        clock=lambda: _NOW,
    )
    submitted: list[Any] = []

    async def submit_authoring_infrastructure_failure(ticket: Any, **_: Any) -> object:
        assert ticket.ticket_id == _TICKET
        submitted.append(ticket)
        platform.events.append("terminal_failure")
        return object()

    worker._coordinator = SimpleNamespace(  # type: ignore[assignment]
        execute=lambda _: (_ for _ in ()).throw(AssertionError("candidate rerun")),
        submit_authoring_infrastructure_failure=submit_authoring_infrastructure_failure,
    )
    assert await worker.run_once() is True
    assert runtime.recoveries == 1
    assert submitted
    assert platform.events == ["claim", "authoring_lease", "terminal_failure"]


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
        instance_id=_INSTANCE,
        run_row_id=_RUN,
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
    worker = CodingShadowWorker(
        platform=platform,
        runtime=_Runtime("terminal_pending"),  # type: ignore[arg-type]
        publication=publication,  # type: ignore[arg-type]
        instance_id=_INSTANCE,
        run_row_id=_RUN,
        clock=lambda: _NOW,
    )
    assert await worker.run_once() is True
    assert platform.events == ["claim", "publish"]
    assert publication.acknowledged == 1
    assert publication.preflights == 1


async def test_claim_from_another_run_is_rejected_before_execution() -> None:
    platform = _Platform(
        _claim(started=False).model_copy(
            update={"run_row_id": UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")}
        )
    )
    worker = CodingShadowWorker(
        platform=platform,
        runtime=_Runtime("released"),  # type: ignore[arg-type]
        publication=_Publication(),  # type: ignore[arg-type]
        instance_id=_INSTANCE,
        run_row_id=_RUN,
        clock=lambda: _NOW,
    )
    with pytest.raises(CodingAttemptIntegrityError, match="authority"):
        await worker.run_once()
    assert platform.events == ["claim"]


async def test_run_forever_logs_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = asyncio.Event()
    worker = CodingShadowWorker(
        platform=_Platform(_claim(started=False)),
        runtime=_Runtime("released"),  # type: ignore[arg-type]
        publication=_Publication(),  # type: ignore[arg-type]
        instance_id=_INSTANCE,
        run_row_id=_RUN,
        clock=lambda: _NOW,
        poll_seconds=1,
    )

    async def boom() -> bool:
        stop.set()
        raise RuntimeError("lease expired for ticket-xyz")

    worker.run_once = boom  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="ditto.validator.coding_worker"):
        await worker.run_forever(stop, drain_requested=asyncio.Event())
    assert "type=RuntimeError" in caplog.text
    assert "lease expired for ticket-xyz" in caplog.text
