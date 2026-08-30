"""Tests for the screener sweep loop (fakes for platform + gate)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from ditto_screener.config import ScreenerConfig
from ditto_screener.errors import PlatformError
from ditto_screener.gate import BuiltImageArtifact, LeaseDeadline
from ditto_screener.heartbeat import (
    DockerHealth,
    ReviewSettingsStatus,
    ScreenerHeartbeatResponse,
)
from ditto_screener.l2_review import L2RunResult, L2Usage
from ditto_screener.policy import (
    PolicyEvidence,
    ScreeningDecision,
    ScreeningOutcome,
    SourceReviewObservation,
    core_decision,
)
from ditto_screener.worker import ScreenerWorker
from ditto_screening_protocol import (
    SCREENING_FLOOR_POLICY_VERSION,
    SCREENING_POLICY_VERSION,
    AgentStatus,
    ArtifactResponse,
    ScreenerQueueItem,
    ScreenerQueueResponse,
    ScreenerReviewSettingsOverride,
    ScreenResultOutcome,
    ScreenReviewAudit,
    SourceReviewFinding,
)

_MINER = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"


def _item(agent_id: UUID, **overrides: Any) -> ScreenerQueueItem:
    overrides.setdefault("lease_deadline", datetime.now(UTC) + timedelta(hours=1))
    return ScreenerQueueItem(
        agent_id=agent_id,
        miner_hotkey=_MINER,
        name="a",
        sha256="de" * 32,
        status=AgentStatus.SCREENING,
        created_at=datetime.now(UTC),
        attempt_id=uuid4(),
        **overrides,
    )


class _FakeKeypair:
    def sign(self, _message: bytes) -> bytes:
        return b"\xcd" * 64


def _decision(outcome: ScreeningOutcome, detail: str = "") -> ScreeningDecision:
    return core_decision(
        outcome,
        code="test",
        summary="test decision",
        detail=detail,
    )


class _FakeGate:
    def __init__(self, result: ScreeningDecision) -> None:
        self.result = result
        self.calls: list[UUID] = []
        self.deadlines: list[float | None] = []
        self.build_only_calls: list[bool] = []
        self.policy_only_calls: list[bool] = []
        self.deferred_source_review_calls: list[bool] = []
        self.policy_versions: list[int] = []
        self.shadow_result: Any = None
        self.applied_review_settings: list[Any] = []

    def apply_review_settings(self, settings: Any) -> bool:
        self.applied_review_settings.append(settings)
        return False

    def pop_shadow_review(self, _attempt_id: UUID) -> Any:
        result, self.shadow_result = self.shadow_result, None
        return result

    async def screen(
        self,
        *,
        agent_id: UUID,
        deadline: float | None = None,
        publish_image: Any = None,
        build_only: bool = False,
        policy_only: bool = False,
        deferred_source_review: bool = False,
        policy_version: int | None = None,
        **_: Any,
    ) -> ScreeningDecision:
        self.calls.append(agent_id)
        self.deadlines.append(deadline)
        self.build_only_calls.append(build_only)
        self.policy_only_calls.append(policy_only)
        self.deferred_source_review_calls.append(deferred_source_review)
        if policy_version is not None:
            self.policy_versions.append(policy_version)
        if (
            self.result.outcome
            in {
                ScreeningOutcome.PASS,
                ScreeningOutcome.PASS_INCONCLUSIVE,
            }
            and publish_image is not None
            and not policy_only
        ):
            await publish_image(
                BuiltImageArtifact(
                    path="/tmp/fake-screened-image.tar",
                    sha256="12" * 32,
                    size_bytes=123,
                    image_id="sha256:" + "34" * 32,
                    image_ref=f"ditto-screen/{agent_id}:latest",
                )
            )
        return self.result


class _FakePlatform:
    def __init__(self, queues: list[list[ScreenerQueueItem]]) -> None:
        self._queues = queues
        self.verdicts: list[dict] = []
        self.submit_error: Exception | None = None
        self.stop_after_queue: asyncio.Event | None = None
        self.required_policy_version = SCREENING_POLICY_VERSION
        self.claim_calls = 0
        self.claimed_policy_versions: list[int] = []
        self.heartbeats: list[Any] = []
        self.heartbeat_error: Exception | None = None
        self.heartbeat_lease_deadline: datetime | None = None
        self.artifact_calls: list[tuple[UUID, UUID | None]] = []
        self.image_uploads: list[dict[str, Any]] = []
        self.review_settings_source = "bootstrap"
        self.review_settings: Any = None
        self.review_settings_revisions: dict[int, Any] = {}
        self.shadow_reviews: list[dict[str, Any]] = []

    async def upload_screened_image(self, agent_id: UUID, **metadata: Any) -> UUID:
        self.image_uploads.append({"agent_id": agent_id, **metadata})
        return UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    async def submit_heartbeat(self, request: Any) -> Any:
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        self.heartbeats.append(request)
        return ScreenerHeartbeatResponse(
            accepted=True,
            seen_at=datetime.now(UTC),
            lease_deadline=self.heartbeat_lease_deadline,
        )

    async def submit_shadow_review(self, agent_id: UUID, request: Any) -> Any:
        self.shadow_reviews.append(
            {"agent_id": agent_id, "request": request.model_dump(mode="json")}
        )
        return object()

    async def get_required_policy_version(self) -> int:
        return self.required_policy_version

    async def get_review_settings(self, _instance_id: str):
        return self.review_settings

    async def get_review_settings_revision(self, revision: int):
        return self.review_settings_revisions[revision]

    async def claim_next(
        self, *, policy_version: int, review_settings: Any, instance_id: str
    ) -> ScreenerQueueResponse:
        del review_settings, instance_id
        self.claim_calls += 1
        self.claimed_policy_versions.append(policy_version)
        items = self._queues.pop(0) if self._queues else []
        # Signal the loop to stop once the queue has drained (first empty sweep),
        # AFTER the item-bearing sweeps have been served + processed.
        if self.stop_after_queue is not None and not items:
            self.stop_after_queue.set()
        return ScreenerQueueResponse(
            items=items,
            count=len(items),
            required_policy_version=policy_version,
        )

    async def get_artifact(
        self, agent_id: UUID, *, attempt_id: UUID | None = None
    ) -> ArtifactResponse:
        self.artifact_calls.append((agent_id, attempt_id))
        return ArtifactResponse(
            agent_id=agent_id,
            sha256="de" * 32,
            download_url="https://storage.test/a.tar.gz",
            expires_at=datetime.now(UTC),
        )

    async def submit_result(  # type: ignore[no-untyped-def]
        self,
        agent_id,
        *,
        signature,
        passed,
        policy_version,
        detail="",
        attempt_id,
        **typed,
    ):
        if self.submit_error is not None:
            raise self.submit_error
        self.verdicts.append(
            {
                "agent_id": agent_id,
                "signature": signature,
                "passed": passed,
                "policy_version": policy_version,
                "detail": detail,
                "attempt_id": attempt_id,
                **typed,
            }
        )

        class _R:
            status = type(
                "S", (), {"value": "evaluating" if passed else "screening_failed"}
            )()

        return _R()


def _worker(cfg: ScreenerConfig, platform, gate, **kwargs: Any) -> ScreenerWorker:  # type: ignore[no-untyped-def]
    if isinstance(platform, _FakePlatform):
        from ditto_screener.review_settings import bootstrap_review_settings

        platform.review_settings = bootstrap_review_settings(cfg)
    return ScreenerWorker(
        config=cfg,
        platform=platform,
        gate=gate,
        keypair=_FakeKeypair(),
        **kwargs,
    )


class _FakeReadiness:
    def __init__(self) -> None:
        self.ready = True

    def set_ready(self) -> None:
        self.ready = True

    def set_unready(self) -> None:
        self.ready = False


async def test_unavailable_rootless_executor_is_autohealed_before_claim(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([[_item(uuid4())]])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    readiness = _FakeReadiness()
    worker = _worker(
        make_config(require_rootless_docker=True),
        platform,
        gate,
        readiness=readiness,
        executor_health_probe=lambda: DockerHealth(
            status="unavailable", running_containers=0, unhealthy_containers=0
        ),
    )

    assert await worker._sweep(asyncio.Event()) == 0
    assert platform.claim_calls == 0
    assert not readiness.ready


async def test_healthy_rootless_executor_can_claim(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([[]])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    readiness = _FakeReadiness()
    readiness.ready = False
    worker = _worker(
        make_config(require_rootless_docker=True),
        platform,
        gate,
        readiness=readiness,
        executor_health_probe=lambda: DockerHealth(
            status="healthy", running_containers=0, unhealthy_containers=0
        ),
    )

    assert await worker._sweep(asyncio.Event()) == 0
    assert platform.claim_calls == 1
    assert readiness.ready


async def test_screen_one_pass_posts_signed_pass_verdict(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    agent = uuid4()
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(_item(agent), policy_version=SCREENING_POLICY_VERSION)
    assert gate.calls == [agent]
    assert len(platform.verdicts) == 1
    v = platform.verdicts[0]
    assert v["passed"] is True and v["signature"] == "cd" * 64 and v["detail"] == ""
    assert v["policy_version"] == SCREENING_POLICY_VERSION
    assert v["attempt_id"] is not None
    assert v["outcome"] == ScreenResultOutcome.PASS
    assert v["image_sha256"] == "12" * 32
    assert v["image_size_bytes"] == 123
    assert v["image_id"] == "sha256:" + "34" * 32
    assert v["image_upload_id"] == UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert len(platform.image_uploads) == 1
    assert platform.heartbeats[0].state == "screening"
    assert platform.heartbeats[0].progress.stage == "preparing"
    assert platform.heartbeats[-1].state == "polling"
    assert platform.heartbeats[-1].progress is None


async def test_attempt_bound_review_override_is_applied_then_restored(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    agent = uuid4()
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)
    normal = platform.review_settings
    canary = normal.model_copy(
        update={
            "revision": 41,
            "scope": "*",
            "settings": normal.settings.model_copy(
                update={
                    "mode": "enforce",
                    "l3_enabled": True,
                    "adjudicator_mode": "enforce",
                }
            ),
        }
    )
    checksum = hashlib.sha256(
        json.dumps(
            canary.settings.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    canary = canary.model_copy(update={"checksum": checksum})
    platform.review_settings_revisions[41] = canary
    item = _item(
        agent,
        review_settings_override=ScreenerReviewSettingsOverride(
            revision=41,
            scope="*",
            checksum=checksum,
        ),
    )

    await worker._screen_one(
        item,
        policy_version=SCREENING_POLICY_VERSION,
        normal_review_settings=normal,
    )

    assert [settings.revision for settings in gate.applied_review_settings] == [41, 0]
    verdict = platform.verdicts[0]
    assert verdict["review_settings_revision"] == 41
    assert verdict["review_settings_scope"] == "*"
    assert verdict["review_settings_checksum"] == checksum


async def test_shadow_review_is_attempt_bound_and_does_not_change_verdict(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    item = _item(uuid4())
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    gate.shadow_result = L2RunResult(
        observation=SourceReviewObservation(
            ok=True,
            risk_level="low",
            finding_digest="ab" * 32,
            categories=("none",),
        ),
        analyzed_files=(),
        causal_path=(),
        tools=(),
        usage=L2Usage(input_tokens=100, output_tokens=10),
        cache_hit=False,
        response_models=("moonshotai/kimi-k3", "openai/gpt-5.6-sol"),
        resolution_basis="authoritative_model_tool_path",
        clearance_path="l3_adjudicated_safe",
        critic_disposition="confirm_safe",
    )
    worker = _worker(make_config(l2_review_mode="shadow"), platform, gate)
    worker._review_settings_status = ReviewSettingsStatus(
        revision=4,
        scope="ditto-screener-prod",
        mode="shadow",
        checksum="cd" * 32,
        source="platform",
    )

    await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)

    assert len(platform.shadow_reviews) == 1
    shadow = platform.shadow_reviews[0]["request"]
    assert shadow["attempt_id"] == str(item.attempt_id)
    assert shadow["artifact_sha256"] == item.sha256
    assert shadow["settings_revision"] == 4
    assert shadow["disposition"] == "safe"
    assert len(platform.verdicts) == 1 and platform.verdicts[0]["passed"] is True


async def test_build_only_item_passes_build_only_to_gate_and_verdict(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    agent = uuid4()
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(
        _item(agent, build_only=True), policy_version=SCREENING_POLICY_VERSION
    )
    assert gate.build_only_calls == [True]
    v = platform.verdicts[0]
    assert v["passed"] is True
    assert v["outcome"] == ScreenResultOutcome.PASS
    assert v["build_only"] is True


async def test_default_item_screens_full_pipeline(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    agent = uuid4()
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(_item(agent), policy_version=SCREENING_POLICY_VERSION)
    assert gate.build_only_calls == [False]
    assert platform.verdicts[0]["build_only"] is False


async def test_policy_only_item_reuses_image_without_upload(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    agent = uuid4()
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)

    await worker._screen_one(
        _item(agent, policy_only=True), policy_version=SCREENING_POLICY_VERSION
    )

    assert gate.policy_only_calls == [True]
    assert platform.image_uploads == []
    verdict = platform.verdicts[0]
    assert verdict["policy_only"] is True
    assert verdict["image_sha256"] is None
    assert verdict["image_upload_id"] is None


async def test_remote_build_gets_full_timeout_despite_stale_local_override(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    """A legacy 20-minute local cap must not reduce Targon to one minute."""
    agent = uuid4()
    platform = _FakePlatform([])
    observed_timeouts: list[float] = []

    async def build_submission_image(  # type: ignore[no-untyped-def]
        _agent_id,
        *,
        attempt_id: UUID,
        timeout,
    ):
        assert attempt_id is not None
        observed_timeouts.append(timeout)
        return None

    platform.build_submission_image = build_submission_image  # type: ignore[attr-defined]
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    original_screen = gate.screen

    async def invoke_remote_build(*, remote_build, **kwargs):  # type: ignore[no-untyped-def]
        await remote_build()
        return await original_screen(**kwargs)

    gate.screen = invoke_remote_build  # type: ignore[method-assign]
    worker = _worker(
        make_config(
            build_timeout_seconds=1200,
            remote_build_mode="prefer",
            remote_build_timeout_seconds=1500,
        ),
        platform,
        gate,
    )

    await worker._screen_one(_item(agent), policy_version=SCREENING_POLICY_VERSION)

    assert observed_timeouts == [1500]


async def test_build_only_quarantine_is_rejected_and_posts_no_verdict(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    # Defense in depth: a build-only run must never quarantine. If the gate
    # regressed and returned one, the worker refuses to submit it.
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.QUARANTINE))
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(
        _item(uuid4(), build_only=True), policy_version=SCREENING_POLICY_VERSION
    )
    assert platform.verdicts == []


async def test_deferred_mechanical_oracle_quarantine_is_submitted(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.QUARANTINE))
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(
        _item(uuid4(), build_only=True, deferred_source_review=True),
        policy_version=SCREENING_POLICY_VERSION,
    )
    assert gate.build_only_calls == [True]
    assert gate.deferred_source_review_calls == [True]
    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    assert verdict["outcome"] == ScreenResultOutcome.QUARANTINE
    assert verdict["build_only"] is True
    assert verdict["deferred_source_review"] is True


async def test_terminal_source_budget_exhaustion_is_signed_and_submitted_once(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    audit = ScreenReviewAudit(
        stage="l1",
        reason_code="source-review-step-budget-exhausted",
        prompt_revision="source-review-v16",
        max_steps=20,
        steps_used=20,
        max_read_bytes=2_000_000,
        read_bytes_used=123_456,
    )
    result = ScreeningDecision(
        outcome=ScreeningOutcome.PASS_INCONCLUSIVE,
        detail="bounded source review inconclusive; admitted for scoring",
        manifest_digest="ab" * 32,
        evidence=(
            PolicyEvidence(
                module_id="luna-source-review",
                code="source-review-inconclusive",
                summary="bounded source review exhausted without a decisive finding",
            ),
        ),
        review_audit=audit.model_dump(mode="json"),
    )
    platform = _FakePlatform([])
    worker = _worker(make_config(), platform, _FakeGate(result))

    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)

    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    assert verdict["passed"] is True
    assert verdict["outcome"] == ScreenResultOutcome.PASS_INCONCLUSIVE
    assert verdict["review_audit_digest"] == audit.canonical_digest()
    assert verdict["reason_code"] == "source-review-inconclusive"


async def test_passing_gate_without_verified_image_posts_no_verdict(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))

    async def skip_publication(*, agent_id: UUID, **_: Any) -> ScreeningDecision:
        gate.calls.append(agent_id)
        return gate.result

    gate.screen = skip_publication  # type: ignore[method-assign]
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)
    assert platform.image_uploads == []
    assert platform.verdicts == []


async def test_screen_one_fail_forwards_detail(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    gate = _FakeGate(
        _decision(ScreeningOutcome.DETERMINISTIC_REJECT, "build failed: E0432")
    )
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)
    v = platform.verdicts[0]
    assert v["passed"] is False and "E0432" in v["detail"]
    assert v["outcome"] == ScreenResultOutcome.DETERMINISTIC_REJECT


async def test_exact_cross_miner_duplicate_skips_artifact_and_private_gate(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    agent_id = uuid4()
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.QUARANTINE))
    worker = _worker(make_config(), platform, gate)

    await worker._screen_one(
        _item(
            agent_id,
            precheck_reason_code="exact-cross-miner-duplicate",
            duplicate_of=uuid4(),
        ),
        policy_version=SCREENING_POLICY_VERSION,
    )

    assert platform.artifact_calls == []
    assert gate.calls == []
    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    assert verdict["outcome"] == ScreenResultOutcome.DETERMINISTIC_REJECT
    assert verdict["reason_code"] == "exact-cross-miner-duplicate"
    assert verdict["detail"] == "exact cross-miner duplicate"


async def test_screen_one_retryable_failure_preserves_v6_screening_failed_verdict(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    gate = _FakeGate(
        _decision(
            ScreeningOutcome.RETRYABLE_INFRA,
            "screener error: Docker daemon temporarily unavailable",
        )
    )
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)
    assert len(platform.verdicts) == 1
    assert platform.verdicts[0]["passed"] is False
    assert platform.verdicts[0]["detail"].startswith("screener error:")
    assert platform.verdicts[0]["outcome"] == ScreenResultOutcome.RETRYABLE_INFRA


async def test_inconclusive_completes_attempt_without_mislabeling_infrastructure(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    gate = _FakeGate(
        _decision(ScreeningOutcome.INCONCLUSIVE, "behavioral oracle inconclusive")
    )
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)
    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    assert verdict["passed"] is False
    assert verdict["outcome"] == ScreenResultOutcome.INCONCLUSIVE
    assert verdict["detail"] == "behavioral oracle inconclusive"


async def test_screen_passes_lease_deadline_budget_to_gate(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)
    item = _item(uuid4(), lease_deadline=datetime.now(UTC) + timedelta(minutes=30))
    await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)
    assert gate.calls == [item.agent_id]
    # The gate receives a monotonic budget bound (not None) derived from the lease.
    assert gate.deadlines[0] is not None
    assert isinstance(gate.deadlines[0], LeaseDeadline)
    assert gate.deadlines[0].expires_at > asyncio.get_running_loop().time()


async def test_accepted_progress_heartbeat_renews_active_local_deadline(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    platform.heartbeat_lease_deadline = datetime.now(UTC) + timedelta(minutes=10)
    worker = _worker(
        make_config(), platform, _FakeGate(_decision(ScreeningOutcome.PASS))
    )
    worker._active_agent_id = uuid4()
    worker._active_progress_stage = "building"
    worker._job_started_at = int(datetime.now(UTC).timestamp())
    worker._active_lease_deadline = LeaseDeadline(asyncio.get_running_loop().time() + 1)

    await worker._report_heartbeat("screening", force=True)

    assert worker._active_lease_deadline.expires_at > (
        asyncio.get_running_loop().time() + 9 * 60
    )


async def test_near_expired_lease_skips_build_and_reports_retryable(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)
    # A lease already at (or past) its deadline: screening cannot land a verdict
    # in time, so the worker must not download or build.
    item = _item(uuid4(), lease_deadline=datetime.now(UTC))
    await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)
    assert gate.calls == []
    assert platform.artifact_calls == []
    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    assert verdict["passed"] is False
    assert verdict["outcome"] == ScreenResultOutcome.RETRYABLE_INFRA
    assert verdict["reason_code"] == "lease-budget-exhausted"


async def test_missing_lease_deadline_leaves_budget_open(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)
    # Legacy platforms omit lease_deadline; the worker must still screen with an
    # open (None) budget rather than treating it as expired.
    item = _item(uuid4(), lease_deadline=None)
    await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)
    assert gate.calls == [item.agent_id]
    assert gate.deadlines[0] is None
    assert platform.verdicts[0]["outcome"] == ScreenResultOutcome.PASS


async def test_quarantine_submits_attempt_bound_typed_result(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    gate = _FakeGate(
        _decision(
            ScreeningOutcome.QUARANTINE,
            "private policy quarantine pending operator review",
        )
    )
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)
    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    assert verdict["passed"] is False
    assert verdict["outcome"].value == "quarantine"
    assert verdict["manifest_digest"]
    assert verdict["reason_code"] == "test"


async def test_verdict_platform_error_swallowed(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    platform.submit_error = PlatformError("409 conflict")
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)
    # Must not raise (a 409/late verdict is logged and skipped).
    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)
    assert platform.verdicts == []


async def test_heartbeat_failure_never_blocks_screening_or_verdict(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    platform.heartbeat_error = PlatformError("heartbeat unavailable")
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)
    assert len(platform.verdicts) == 1
    assert gate.calls
    assert worker._active_agent_id is None
    assert worker._active_progress_stage is None
    assert worker._job_started_at is None


async def test_run_forever_drains_queue_then_stops(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    a1, a2 = uuid4(), uuid4()
    # First sweep has two agents; the second (empty) sweep trips the stop.
    platform = _FakePlatform([[_item(a1), _item(a2)], []])
    stop = asyncio.Event()
    platform.stop_after_queue = stop  # set on the first empty sweep
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)
    await asyncio.wait_for(worker.run_forever(stop), timeout=2.0)
    assert gate.calls == [a1, a2]
    assert {v["agent_id"] for v in platform.verdicts} == {a1, a2}


async def test_run_forever_exits_immediately_when_stopped(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(worker.run_forever(stop), timeout=2.0)


async def test_policy_below_floor_does_not_claim(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([[_item(uuid4())]])
    platform.required_policy_version = SCREENING_POLICY_VERSION - 2
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)

    try:
        await worker._sweep(asyncio.Event())
    except PlatformError as exc:
        assert "older than this build supports" in str(exc)
    else:
        raise AssertionError("policy mismatch must stop before claiming")

    assert platform.claim_calls == 0
    assert gate.calls == []


async def test_policy_newer_than_build_does_not_claim(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([[_item(uuid4())]])
    platform.required_policy_version = SCREENING_POLICY_VERSION + 1
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)

    try:
        await worker._sweep(asyncio.Event())
    except PlatformError as exc:
        assert "newer than this build" in str(exc)
        assert "activation" in str(exc)
    else:
        raise AssertionError("unimplemented policy must stop before claiming")

    assert platform.claim_calls == 0
    assert gate.calls == []


async def test_floor_policy_claims_and_signs_floor_policy(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([[_item(uuid4())]])
    platform.required_policy_version = SCREENING_FLOOR_POLICY_VERSION
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)

    assert await worker._sweep(asyncio.Event()) == 1
    assert platform.claimed_policy_versions == [SCREENING_FLOOR_POLICY_VERSION]
    assert gate.policy_versions == [SCREENING_FLOOR_POLICY_VERSION]
    assert platform.verdicts[0]["policy_version"] == SCREENING_FLOOR_POLICY_VERSION
    assert platform.heartbeats
    assert all(
        heartbeat.policy_version == SCREENING_FLOOR_POLICY_VERSION
        for heartbeat in platform.heartbeats
    )


async def test_floor_policy_corrects_idle_heartbeat_before_claim(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([[]])
    platform.required_policy_version = SCREENING_FLOOR_POLICY_VERSION
    worker = _worker(
        make_config(), platform, _FakeGate(_decision(ScreeningOutcome.PASS))
    )

    assert await worker._sweep(asyncio.Event()) == 0
    assert platform.claimed_policy_versions == [SCREENING_FLOOR_POLICY_VERSION]
    assert [heartbeat.state for heartbeat in platform.heartbeats] == ["polling"]
    assert [heartbeat.policy_version for heartbeat in platform.heartbeats] == [
        SCREENING_FLOOR_POLICY_VERSION
    ]


async def test_current_policy_claims_and_signs_current_policy(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([[_item(uuid4())]])
    platform.required_policy_version = SCREENING_POLICY_VERSION
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)

    assert await worker._sweep(asyncio.Event()) == 1
    assert platform.verdicts[0]["policy_version"] == SCREENING_POLICY_VERSION


async def test_quarantine_ships_bounded_evidence_and_digest_bound_finding(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    finding = SourceReviewFinding(
        artifact_sha256="de" * 32,
        prompt_revision="source-review-v2",
        risk_level="high",
        confidence=0.97,
        categories=["benchmark_emulation"],
        evidence=[
            {"path": "src/main.rs", "line": 42, "category": "benchmark_emulation"}
        ],
        summary="Deterministic shortcut bypasses the general provider path.",
    )
    decision = ScreeningDecision(
        outcome=ScreeningOutcome.QUARANTINE,
        detail="private policy quarantine pending operator review",
        manifest_digest="ab" * 32,
        evidence=(
            PolicyEvidence(
                "luna-source-review",
                "agentic-source-review-tripwire",
                "private source analysis selected a behavioral audit",
                finding.canonical_digest(),
            ),
        ),
        finding=finding.model_dump(mode="json"),
    )
    platform = _FakePlatform([])
    worker = _worker(make_config(), platform, _FakeGate(decision))
    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)

    verdict = platform.verdicts[0]
    assert verdict["outcome"].value == "quarantine"
    assert verdict["reason_code"] == "agentic-source-review-tripwire"
    # The signed finding_digest binds the shipped finding payload exactly.
    assert verdict["finding_digest"] == finding.canonical_digest()
    assert verdict["finding"].canonical_digest() == verdict["finding_digest"]
    assert [item.code for item in verdict["evidence"]] == [
        "agentic-source-review-tripwire"
    ]
    assert verdict["evidence"][0].digest == finding.canonical_digest()


async def test_quarantine_without_finding_keeps_last_evidence_digest(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    decision = ScreeningDecision(
        outcome=ScreeningOutcome.QUARANTINE,
        detail="private policy quarantine pending operator review",
        manifest_digest="ab" * 32,
        evidence=(
            PolicyEvidence(
                "v8-behavioral-oracle",
                "behavioral-oracle-wrong-answer",
                "behavioral oracle final answer did not match the "
                "gateway-encoded value",
                "cd" * 32,
            ),
        ),
    )
    platform = _FakePlatform([])
    worker = _worker(make_config(), platform, _FakeGate(decision))
    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)

    verdict = platform.verdicts[0]
    assert verdict["finding"] is None
    assert verdict["finding_digest"] == "cd" * 32
    assert verdict["evidence"][0].module_id == "v8-behavioral-oracle"
