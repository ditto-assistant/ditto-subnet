"""Tests for the unused shadow coding attempt coordinator."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from ditto.api_models.coding import (
    CodingAuthoringEvidence,
    CodingAuthoringLeaseResponse,
    CodingGradingLeaseResponse,
    CodingRunEvidence,
    CodingTaskEvidence,
    SubmitCodingAuthoringFreezeResponse,
    SubmitCodingShadowResultResponse,
    coding_authoring_evidence_digest,
    validate_run_evidence_against_manifest,
)
from ditto.api_models.coding_harness import CodingHarnessLaunchResponse
from ditto.validator.coding_attempt import (
    CodingAttemptCoordinator,
    CodingAttemptExpiredError,
    CodingAttemptIntegrityError,
    CodingAttemptTicket,
    CodingAuthoringOutcome,
    CodingGradingOutcome,
)

_ROOT = Path(__file__).parents[3]
_TESTDATA = _ROOT / "packages" / "dittobench-coding-contract" / "testdata"
_SELECTION = _TESTDATA / "coding_selection_v1.json"
_ARTIFACTS = _TESTDATA / "coding_artifact_capability_v1.json"
_FREEZE = _TESTDATA / "coding_authoring_freeze_v1.json"
_GRADING = _TESTDATA / "coding_grading_lease_v1.json"
_RESULT = _TESTDATA / "coding_shadow_result_submission_v1.json"
_EXECUTION = _TESTDATA / "coding_execution_plan_v1.json"


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _authoring_lease() -> CodingAuthoringLeaseResponse:
    selection = _json(_SELECTION)
    artifacts = _json(_ARTIFACTS)
    execution = _json(_EXECUTION)
    task = selection["task_version"]["payload"]
    raw = {
        "schema": "dittobench-coding-authoring-lease-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "ticket_id": artifacts["capabilities"][0]["ticket_id"],
        "ticket_deadline": artifacts["capabilities"][0]["ticket_deadline"],
        "coding_run_id": selection["run_manifest"]["coding_run_id"],
        "run_manifest_sha256": selection["run_authority"]["run_manifest_sha256"],
        "task_set_manifest_sha256": selection["run_manifest"][
            "task_set_manifest_sha256"
        ],
        "repository_epoch": task["repository_epoch"],
        "issue_sha256": task["issue_sha256"],
        "runtime_policy_sha256": task["runtime_policy_sha256"],
        "budgets_sha256": task["budgets_sha256"],
        "issue": selection["issue"],
        "runtime_policy": selection["runtime_policy"],
        "budgets": selection["budgets"],
        "runner_plan_sha256": execution["expected"]["runner_plan_sha256"],
        "runner_plan": execution["runner_plan"],
        "run_manifest": selection["run_manifest"],
        "capabilities": artifacts["capabilities"][:3],
    }
    return CodingAuthoringLeaseResponse.model_validate_json(json.dumps(raw))


def _authoring_outcome() -> CodingAuthoringOutcome:
    vector = _json(_FREEZE)
    raw = vector["request"]
    evidence = CodingAuthoringEvidence.model_validate_json(json.dumps(raw["evidence"]))
    model = evidence.model.model_copy(update={"inference_grant_sha256": "44" * 32})
    evidence = evidence.model_copy(update={"model": model})
    return CodingAuthoringOutcome(
        evidence=evidence,
        authoring_transcript_object_key=raw["authoring_transcript_object_key"],
        authoring_transcript_bytes=raw["authoring_transcript_bytes"],
        authoring_event_count=raw["authoring_event_count"],
        frozen_submission_object_key=raw["frozen_submission_object_key"],
        capabilities_revoked=True,
        authoring_environment_destroyed=True,
    )


def _freeze_response(
    ticket: CodingAttemptTicket,
    authoring: CodingAuthoringOutcome,
) -> SubmitCodingAuthoringFreezeResponse:
    return SubmitCodingAuthoringFreezeResponse(
        freeze_id=UUID("44444444-4444-4444-8444-444444444444"),
        agent_id=ticket.agent_id,
        run_row_id=ticket.run_row_id,
        ticket_id=ticket.ticket_id,
        coding_run_id="coding-run-private-001",
        authoring_evidence_sha256=coding_authoring_evidence_digest(authoring.evidence),
        frozen_at=datetime(2026, 8, 21, 12, 30, tzinfo=UTC),
        accepted=True,
        idempotent=False,
        weight_eligible=False,
    )


def _grading_lease(
    authoring: CodingAuthoringOutcome,
) -> CodingGradingLeaseResponse:
    raw = _json(_GRADING)["response"]
    raw["authoring_evidence_sha256"] = coding_authoring_evidence_digest(
        authoring.evidence
    )
    return CodingGradingLeaseResponse.model_validate_json(json.dumps(raw))


def _grading_outcome() -> CodingGradingOutcome:
    vector = _json(_RESULT)
    manifest = _json(_SELECTION)["run_manifest"]
    selected_task = manifest["tasks"][0]
    tasks = tuple(
        CodingTaskEvidence.model_validate_json(
            json.dumps(
                {
                    **item,
                    "coding_run_id": manifest["coding_run_id"],
                    "agent_id": manifest["agent_id"],
                    "agent_artifact_sha256": manifest["agent_artifact_sha256"],
                    "corpus_release_id": manifest["corpus_release_id"],
                    "task_set_id": manifest["task_set_id"],
                    "task_set_manifest_sha256": manifest["task_set_manifest_sha256"],
                    "task": selected_task,
                }
            )
        )
        for item in vector["authority"]["task_evidence"]
    )
    return CodingGradingOutcome(
        task_evidence=tasks,
        grading_environment_destroyed=True,
    )


def _result_response() -> SubmitCodingShadowResultResponse:
    return SubmitCodingShadowResultResponse.model_validate_json(
        json.dumps(_json(_RESULT)["response"])
    )


def _ticket() -> CodingAttemptTicket:
    lease = _authoring_lease()
    return CodingAttemptTicket(
        agent_id=UUID("00000000-0000-4000-8000-000000000001"),
        bench_version=12,
        run_row_id=UUID("22222222-2222-4222-8222-222222222222"),
        ticket_id=lease.ticket_id,
        ticket_deadline=lease.ticket_deadline,
        agent_artifact_sha256="55" * 32,
        screened_image_sha256="66" * 32,
    )


def _harness_launch() -> CodingHarnessLaunchResponse:
    ticket = _ticket()
    return CodingHarnessLaunchResponse(
        schema="dittobench-coding-harness-launch-v1",
        coding_contract_version=1,
        weight_eligible=False,
        agent_id=ticket.agent_id,
        run_row_id=ticket.run_row_id,
        ticket_id=ticket.ticket_id,
        ticket_deadline=ticket.ticket_deadline,
        bench_version=ticket.bench_version,
        agent_artifact_sha256=ticket.agent_artifact_sha256,
        screened_image_sha256=ticket.screened_image_sha256,
        screened_image_size_bytes=1024,
        screened_image_id="sha256:" + "77" * 32,
        screened_image_ref=f"ditto-screen/{ticket.agent_id}:latest",
        screening_policy_version=9,
        image_url="https://storage.invalid/screened.tar?signature=test",
        expires_at=datetime(2026, 8, 21, 12, 5, tzinfo=UTC),
    )


class _Platform:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.authoring_lease = _authoring_lease()
        self.authoring_outcome = _authoring_outcome()
        self.freeze_response = _freeze_response(_ticket(), self.authoring_outcome)
        self.grading_lease = _grading_lease(self.authoring_outcome)
        self.result_response = _result_response()
        self.harness_launch = _harness_launch()

    async def request_coding_authoring_lease(
        self,
        ticket_id: UUID,
    ) -> CodingAuthoringLeaseResponse:
        assert ticket_id == _ticket().ticket_id
        self.events.append("request_authoring")
        return self.authoring_lease

    async def request_coding_harness_launch(
        self,
        ticket_id: UUID,
    ) -> CodingHarnessLaunchResponse:
        assert ticket_id == _ticket().ticket_id
        self.events.append("request_harness")
        return self.harness_launch

    async def submit_coding_authoring_freeze(
        self,
        agent_id: UUID,
        **kwargs: Any,
    ) -> SubmitCodingAuthoringFreezeResponse:
        assert agent_id == _ticket().agent_id
        assert kwargs["evidence"] == self.authoring_outcome.evidence
        self.events.append("submit_freeze")
        return self.freeze_response

    async def request_coding_grading_lease(
        self,
        **kwargs: Any,
    ) -> CodingGradingLeaseResponse:
        assert kwargs["freeze_id"] == self.freeze_response.freeze_id
        assert kwargs["expected_frozen_patch_sha256"] == "bb" * 32
        self.events.append("request_grading")
        return self.grading_lease

    async def submit_coding_shadow_result(
        self,
        agent_id: UUID,
        **kwargs: Any,
    ) -> SubmitCodingShadowResultResponse:
        assert agent_id == _ticket().agent_id
        assert kwargs["run_manifest"] == self.grading_lease.run_manifest
        evidence = CodingRunEvidence.model_validate(kwargs["evidence"])
        task_evidence = [
            CodingTaskEvidence.model_validate(item) for item in kwargs["task_evidence"]
        ]
        validate_run_evidence_against_manifest(
            self.grading_lease.run_manifest,
            str(self.grading_lease.ticket_id),
            evidence,
            task_evidence,
        )
        self.events.append("submit_result")
        return self.result_response


class _Runtime:
    def __init__(self, events: list[str], platform: _Platform) -> None:
        self.events = events
        self.platform = platform

    async def author(
        self,
        lease: CodingAuthoringLeaseResponse,
        harness: CodingHarnessLaunchResponse,
    ) -> CodingAuthoringOutcome:
        assert lease == self.platform.authoring_lease
        assert harness == self.platform.harness_launch
        self.events.append("author")
        return self.platform.authoring_outcome

    async def grade(
        self,
        lease: CodingGradingLeaseResponse,
        authoring: CodingAuthoringOutcome,
    ) -> CodingGradingOutcome:
        assert lease == self.platform.grading_lease
        assert authoring.capabilities_revoked is True
        assert authoring.authoring_environment_destroyed is True
        self.events.append("grade")
        return _grading_outcome()

    async def abort_authoring(self, lease: CodingAuthoringLeaseResponse) -> None:
        assert lease == self.platform.authoring_lease
        self.events.append("abort_authoring")

    async def abort_grading(self, lease: CodingGradingLeaseResponse) -> None:
        assert lease == self.platform.grading_lease
        self.events.append("abort_grading")


class _FailingRuntime(_Runtime):
    def __init__(
        self,
        events: list[str],
        platform: _Platform,
        *,
        failure_phase: str,
    ) -> None:
        super().__init__(events, platform)
        self.failure_phase = failure_phase

    async def author(
        self,
        lease: CodingAuthoringLeaseResponse,
        harness: CodingHarnessLaunchResponse,
    ) -> CodingAuthoringOutcome:
        if self.failure_phase == "authoring":
            self.events.append("author")
            raise RuntimeError("synthetic authoring failure")
        return await super().author(lease, harness)

    async def grade(
        self,
        lease: CodingGradingLeaseResponse,
        authoring: CodingAuthoringOutcome,
    ) -> CodingGradingOutcome:
        if self.failure_phase == "grading":
            self.events.append("grade")
            raise RuntimeError("synthetic grading failure")
        return await super().grade(lease, authoring)


def _coordinator(
    *,
    clock,
) -> tuple[CodingAttemptCoordinator, _Platform, list[str]]:
    events: list[str] = []
    platform = _Platform(events)
    runtime = _Runtime(events, platform)
    return (
        CodingAttemptCoordinator(platform=platform, runtime=runtime, clock=clock),
        platform,
        events,
    )


async def test_coordinator_enforces_freeze_before_grader_and_result() -> None:
    coordinator, platform, events = _coordinator(
        clock=lambda: datetime(2026, 8, 21, 12, tzinfo=UTC)
    )
    accepted = await coordinator.execute(_ticket())
    assert accepted == platform.result_response
    assert events == [
        "request_authoring",
        "request_harness",
        "author",
        "submit_freeze",
        "request_grading",
        "grade",
        "submit_result",
    ]


async def test_expired_ticket_starts_nothing() -> None:
    coordinator, _platform, events = _coordinator(
        clock=lambda: datetime(2026, 8, 21, 13, tzinfo=UTC)
    )
    with pytest.raises(CodingAttemptExpiredError, match="authoring"):
        await coordinator.execute(_ticket())
    assert events == []


async def test_ticket_expiring_after_authoring_still_freezes_before_stopping() -> None:
    times = iter(
        (
            datetime(2026, 8, 21, 12, tzinfo=UTC),
            datetime(2026, 8, 21, 13, tzinfo=UTC),
        )
    )
    coordinator, _platform, events = _coordinator(clock=lambda: next(times))
    with pytest.raises(CodingAttemptExpiredError, match="grading"):
        await coordinator.execute(_ticket())
    assert events == ["request_authoring", "request_harness", "author", "submit_freeze"]


async def test_authoring_lease_identity_drift_reaches_no_runtime() -> None:
    coordinator, platform, events = _coordinator(
        clock=lambda: datetime(2026, 8, 21, 12, tzinfo=UTC)
    )
    manifest = platform.authoring_lease.run_manifest.model_copy(
        update={"agent_id": "00000000-0000-4000-8000-000000000002"}
    )
    platform.authoring_lease = platform.authoring_lease.model_copy(
        update={"run_manifest": manifest}
    )
    with pytest.raises(CodingAttemptIntegrityError, match="authoring lease"):
        await coordinator.execute(_ticket())
    assert events == ["request_authoring"]


async def test_harness_launch_identity_drift_reaches_no_runtime() -> None:
    coordinator, platform, events = _coordinator(
        clock=lambda: datetime(2026, 8, 21, 12, tzinfo=UTC)
    )
    platform.harness_launch = platform.harness_launch.model_copy(
        update={"screened_image_sha256": "ff" * 32}
    )
    with pytest.raises(CodingAttemptIntegrityError, match="harness launch"):
        await coordinator.execute(_ticket())
    assert events == ["request_authoring", "request_harness"]


@pytest.mark.parametrize("drift", ["freeze", "patch"])
async def test_grading_lease_drift_reaches_no_grader(drift: str) -> None:
    coordinator, platform, events = _coordinator(
        clock=lambda: datetime(2026, 8, 21, 12, tzinfo=UTC)
    )
    update: dict[str, Any]
    if drift == "freeze":
        update = {"freeze_id": UUID("99999999-9999-4999-8999-999999999999")}
    else:
        update = {
            "frozen_patch_sha256": "cc" * 32,
            "frozen_submission_object_key": "sha256/" + "cc" * 32,
        }
    platform.grading_lease = platform.grading_lease.model_copy(update=update)
    with pytest.raises(CodingAttemptIntegrityError, match="grading lease"):
        await coordinator.execute(_ticket())
    assert events == [
        "request_authoring",
        "request_harness",
        "author",
        "submit_freeze",
        "request_grading",
    ]


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (
            "authoring",
            ["request_authoring", "request_harness", "author", "abort_authoring"],
        ),
        (
            "grading",
            [
                "request_authoring",
                "request_harness",
                "author",
                "submit_freeze",
                "request_grading",
                "grade",
                "abort_grading",
            ],
        ),
    ],
)
async def test_runtime_failure_triggers_phase_cleanup(
    phase: str,
    expected: list[str],
) -> None:
    events: list[str] = []
    platform = _Platform(events)
    runtime = _FailingRuntime(events, platform, failure_phase=phase)
    coordinator = CodingAttemptCoordinator(
        platform=platform,
        runtime=runtime,
        clock=lambda: datetime(2026, 8, 21, 12, tzinfo=UTC),
    )
    with pytest.raises(RuntimeError, match="synthetic"):
        await coordinator.execute(_ticket())
    assert events == expected


def test_outcomes_require_revocation_cleanup_and_authoritative_activity() -> None:
    valid = _authoring_outcome()
    with pytest.raises(CodingAttemptIntegrityError, match="not gradeable"):
        CodingAuthoringOutcome(
            evidence=valid.evidence,
            authoring_transcript_object_key=valid.authoring_transcript_object_key,
            authoring_transcript_bytes=valid.authoring_transcript_bytes,
            authoring_event_count=valid.authoring_event_count,
            frozen_submission_object_key=valid.frozen_submission_object_key,
            capabilities_revoked=False,  # type: ignore[arg-type]
            authoring_environment_destroyed=True,
        )
    with pytest.raises(CodingAttemptIntegrityError, match="not gradeable"):
        CodingAuthoringOutcome(
            evidence=valid.evidence,
            authoring_transcript_object_key=valid.authoring_transcript_object_key,
            authoring_transcript_bytes=True,  # type: ignore[arg-type]
            authoring_event_count=valid.authoring_event_count,
            frozen_submission_object_key=valid.frozen_submission_object_key,
            capabilities_revoked=True,
            authoring_environment_destroyed=True,
        )
    with pytest.raises(CodingAttemptIntegrityError, match="incomplete"):
        CodingGradingOutcome(
            task_evidence=(),
            grading_environment_destroyed=True,
        )


def test_ticket_rejects_boolean_version_and_zero_identity() -> None:
    valid = _ticket()
    with pytest.raises(CodingAttemptIntegrityError, match="ticket"):
        CodingAttemptTicket(
            agent_id=UUID(int=0),
            bench_version=True,  # type: ignore[arg-type]
            run_row_id=valid.run_row_id,
            ticket_id=valid.ticket_id,
            ticket_deadline=valid.ticket_deadline,
            agent_artifact_sha256=valid.agent_artifact_sha256,
            screened_image_sha256=valid.screened_image_sha256,
        )
