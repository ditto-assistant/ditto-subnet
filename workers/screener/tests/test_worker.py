"""Tests for the screener sweep loop (fakes for platform + gate)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from ditto_screener import worker as worker_module
from ditto_screener.adjudicator import SourceReviewAdjudicator
from ditto_screener.config import ScreenerConfig
from ditto_screener.errors import (
    PlatformAuthOnlyFailure,
    PlatformError,
    PlatformRejected,
)
from ditto_screener.gate import BuiltImageArtifact, LeaseDeadline
from ditto_screener.heartbeat import (
    DockerHealth,
    HostSpecs,
    ReviewSettingsStatus,
    ScreenerHeartbeatResponse,
)
from ditto_screener.l2_review import L2RunResult, L2Usage
from ditto_screener.platform import PlatformClient
from ditto_screener.policy import (
    CORE_ONLY_MANIFEST,
    SOURCE_REVIEW_KEY_UNAVAILABLE_CODE,
    PolicyEngine,
    PolicyEvidence,
    ScreeningDecision,
    ScreeningOutcome,
    SourceReviewObservation,
    core_decision,
    is_held_source_review,
)
from ditto_screener.worker import ScreenerWorker, _verdict_reason_code
from ditto_screening_protocol import (
    SCREENING_FLOOR_POLICY_VERSION,
    SCREENING_POLICY_VERSION,
    AdjudicationCompletionReceipt,
    AgentStatus,
    ArtifactResponse,
    ScreenerQueueItem,
    ScreenerQueueResponse,
    ScreenerReviewSettingsOverride,
    ScreenResultOutcome,
    ScreenResultRequest,
    ScreenReviewAudit,
    SourceReviewAdjudication,
    SourceReviewCitation,
    SourceReviewFinding,
    SourceReviewNote,
    source_review_notes_digest,
)
from ditto_screening_protocol.reason_codes import (
    SOURCE_REVIEW_PROVIDER_CREDITS_EXHAUSTED,
)

_MINER = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"


def _item(agent_id: UUID, **overrides: Any) -> ScreenerQueueItem:
    overrides.setdefault("lease_deadline", datetime.now(UTC) + timedelta(hours=1))
    overrides.setdefault("policy_version", SCREENING_POLICY_VERSION)
    overrides.setdefault("bench_version", 12)
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
    def __init__(
        self, result: ScreeningDecision, *, bind_policy_version: bool = True
    ) -> None:
        self.result = result
        self.bind_policy_version = bind_policy_version
        self.calls: list[UUID] = []
        self.deadlines: list[float | None] = []
        self.build_only_calls: list[bool] = []
        self.policy_only_calls: list[bool] = []
        self.deferred_source_review_calls: list[bool] = []
        self.policy_versions: list[int] = []
        self.bench_versions: list[int] = []
        self.shadow_result: Any = None
        self.applied_review_settings: list[Any] = []
        self.received_at: list[int | None] = []

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
        publish_held_image: Any = None,
        record_archive_verification: Any = None,
        build_only: bool = False,
        policy_only: bool = False,
        deferred_source_review: bool = False,
        policy_version: int | None = None,
        bench_version: int | None = None,
        scored_runtime_evidence_received_at: int | None = None,
        **_: Any,
    ) -> ScreeningDecision:
        self.calls.append(agent_id)
        self.received_at.append(scored_runtime_evidence_received_at)
        self.deadlines.append(deadline)
        self.build_only_calls.append(build_only)
        self.policy_only_calls.append(policy_only)
        self.deferred_source_review_calls.append(deferred_source_review)
        if policy_version is not None:
            self.policy_versions.append(policy_version)
        if bench_version is not None:
            self.bench_versions.append(bench_version)
        if record_archive_verification is not None:
            await record_archive_verification()
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
        if publish_held_image is not None and is_held_source_review(self.result):
            await publish_held_image(
                BuiltImageArtifact(
                    path="/tmp/fake-held-image.tar",
                    sha256="12" * 32,
                    size_bytes=123,
                    image_id="sha256:" + "34" * 32,
                    image_ref=f"ditto-screen/{agent_id}:latest",
                )
            )
        if self.bind_policy_version and policy_version is not None:
            return replace(self.result, policy_version=policy_version)
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
        self.artifact_error: Exception | None = None
        self.heartbeat_lease_deadline: datetime | None = None
        self.heartbeat_fixture_supported = False
        self.artifact_calls: list[tuple[UUID, UUID | None]] = []
        self.image_uploads: list[dict[str, Any]] = []
        self.verification_receipts: list[dict[str, Any]] = []
        self.review_settings_source = "bootstrap"
        self.review_settings: Any = None
        self.review_settings_revisions: dict[int, Any] = {}
        self.shadow_reviews: list[dict[str, Any]] = []

    async def upload_screened_image(self, agent_id: UUID, **metadata: Any) -> UUID:
        self.image_uploads.append({"agent_id": agent_id, **metadata})
        return UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    async def record_verification_receipt(self, agent_id: UUID, **receipt: Any) -> None:
        self.verification_receipts.append({"agent_id": agent_id, **receipt})

    async def submit_heartbeat(self, request: Any) -> Any:
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        self.heartbeats.append(request)
        return ScreenerHeartbeatResponse(
            accepted=True,
            seen_at=datetime.now(UTC),
            lease_deadline=self.heartbeat_lease_deadline,
            source_fixture_v1_heartbeat_supported=self.heartbeat_fixture_supported,
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
        if self.artifact_error is not None:
            raise self.artifact_error
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


async def test_configured_instance_id_distinguishes_local_worker_heartbeat(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    worker = _worker(
        make_config(
            node_id="subnet-screener-1",
            instance_id="subnet-screener-1-worker-2",
        ),
        platform,
        _FakeGate(_decision(ScreeningOutcome.PASS)),
    )

    await worker._report_heartbeat("polling", force=True)

    assert platform.heartbeats[-1].instance_id == "subnet-screener-1-worker-2"


async def test_fixture_heartbeat_capability_waits_for_platform_ack(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    worker = _worker(
        make_config(node_id="subnet-screener-1"),
        platform,
        _FakeGate(_decision(ScreeningOutcome.PASS)),
        host_specs_probe=lambda: HostSpecs(
            cpu_count=4,
            memory_total_mib=8000,
            disk_total_gib=80,
            architecture="x86_64",
        ),
    )
    await worker._report_heartbeat("polling", force=True)
    assert platform.heartbeats[-1].protocol_version == 7
    assert platform.heartbeats[-1].release.source_fixture_v1 is False
    platform.heartbeat_fixture_supported = True
    await worker._report_heartbeat("polling", force=True)
    assert platform.heartbeats[-1].protocol_version == 7
    await worker._report_heartbeat("polling", force=True)
    assert platform.heartbeats[-1].protocol_version == 8
    assert platform.heartbeats[-1].release.source_fixture_v1 is True
    platform.heartbeat_error = RuntimeError("rolling old Platform")
    await worker._report_heartbeat("polling", force=True)
    platform.heartbeat_error = None
    await worker._report_heartbeat("polling", force=True)
    assert platform.heartbeats[-1].protocol_version == 7


def test_legacy_node_instance_id_derives_the_systemd_worker_index(
    monkeypatch: pytest.MonkeyPatch,
    make_config: Callable[..., ScreenerConfig],
) -> None:
    monkeypatch.setattr(
        "ditto_screener.worker.Path.read_text",
        lambda *_args, **_kwargs: "0::/system.slice/ditto-screener-worker@2.service\n",
    )

    worker = _worker(
        make_config(node_id="subnet-screener-1", instance_id="subnet-screener-1"),
        _FakePlatform([]),
        _FakeGate(_decision(ScreeningOutcome.PASS)),
    )

    assert worker._instance_id == "subnet-screener-1-worker-2"


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


async def test_report_only_canary_emits_active_progress_and_clears_heartbeat(
    make_config: Callable[..., ScreenerConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ditto_screener import l2_report_canary

    agent_id = uuid4()
    platform = _FakePlatform([[]])
    worker = _worker(
        make_config(), platform, _FakeGate(_decision(ScreeningOutcome.PASS))
    )

    async def consume(**kwargs: Any) -> bool:
        kwargs["on_claim"](type("Claim", (), {"agent_id": agent_id})())
        kwargs["progress"]("source_review_0")
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(l2_report_canary, "consume", consume)
    assert await worker._sweep(asyncio.Event()) == 1
    assert any(
        beat.state == "screening"
        and beat.active_agent_id == agent_id
        and beat.progress is not None
        for beat in platform.heartbeats
    )
    assert platform.heartbeats[-1].state == "polling"
    assert platform.heartbeats[-1].active_agent_id is None


async def test_pinned_canary_leaves_primary_gate_and_next_claim_on_node_settings(
    make_config: Callable[..., ScreenerConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator canary posture never leaks into the authoritative lane."""
    from ditto_screener import l2_report_canary

    from .test_l2_report_canary import _pinned_claim, _pinned_posture

    class Platform(_FakePlatform):
        def __init__(self, queues: list[list[ScreenerQueueItem]]) -> None:
            super().__init__(queues)
            self.primary_claim_settings: list[Any] = []
            self.canary_claim: dict[str, Any] | None = None
            self.canary_completions: list[dict[str, Any]] = []

        async def claim_next(
            self, *, policy_version: int, review_settings: Any, instance_id: str
        ) -> ScreenerQueueResponse:
            self.primary_claim_settings.append(review_settings)
            return await super().claim_next(
                policy_version=policy_version,
                review_settings=review_settings,
                instance_id=instance_id,
            )

        async def claim_l2_report_canary(self, **_kwargs: Any) -> Any:
            claim, self.canary_claim = self.canary_claim, None
            return claim

        async def complete_l2_report_canary(self, *_args: Any, **kwargs: Any) -> None:
            self.canary_completions.append(kwargs)

    agent = uuid4()
    # Sweep 1 finds the primary queue empty and runs the pinned canary; sweep 2
    # screens an authoritative attempt.
    platform = Platform([[], [_item(agent)]])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    gate._client = object()  # type: ignore[attr-defined]
    worker = _worker(make_config(), platform, gate)
    node = platform.review_settings
    pin = _pinned_posture(node)
    platform.review_settings_revisions[pin.revision] = pin
    platform.canary_claim = _pinned_claim(pin)
    canary_configs: list[ScreenerConfig] = []

    class CanaryGate:
        def __init__(self, canary_config: ScreenerConfig, *_a: Any, **_k: Any):
            canary_configs.append(canary_config)

        async def screen(self, **_kwargs: Any) -> ScreeningDecision:
            return _decision(ScreeningOutcome.INCONCLUSIVE)

        def pop_shadow_review(self, _attempt_id: UUID) -> L2RunResult:
            return L2RunResult(
                observation=SourceReviewObservation(
                    ok=True, risk_level="low", finding_digest=None, categories=()
                ),
                analyzed_files=(),
                causal_path=(),
                tools=(),
                usage=L2Usage(),
                cache_hit=False,
            )

        def pop_preview_l1_review(self, _attempt_id: UUID) -> None:
            return None

    monkeypatch.setattr(l2_report_canary, "BuildGate", CanaryGate)
    monkeypatch.setattr(
        l2_report_canary, "load_policy_engine", lambda *_a, **_k: object()
    )

    assert await worker._sweep(asyncio.Event()) == 1
    (completion,) = platform.canary_completions
    assert completion["status"] == "succeeded"
    assert completion["report"]["settings_revision"] == pin.revision
    assert canary_configs[0].l2_timeout_seconds == 777.0

    assert await worker._sweep(asyncio.Event()) == 1
    assert gate.calls == [agent]
    # Both primary claims, including the one right after the pinned canary,
    # carry the node posture, and the primary gate never saw the pin.
    assert [s.revision for s in platform.primary_claim_settings] == [
        node.revision,
        node.revision,
    ]
    assert gate.applied_review_settings
    assert all(
        (s.revision, s.checksum) == (node.revision, node.checksum)
        for s in gate.applied_review_settings
    )
    assert worker._review_settings_status.revision == node.revision
    assert worker._review_settings_status.checksum == node.checksum


async def test_screen_one_pass_posts_signed_pass_verdict(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    agent = uuid4()
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(_item(agent), policy_version=SCREENING_POLICY_VERSION)
    assert gate.calls == [agent]
    assert gate.bench_versions == [12]
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
    assert {row["check_code"] for row in platform.verification_receipts} == {
        "archive_sha",
        "build_image_digest",
    }
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


@pytest.fixture
def shadow_review_case(
    make_config: Callable[..., ScreenerConfig],
) -> tuple[ScreenerWorker, _FakePlatform, _FakeGate, ScreenerQueueItem]:
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
        response_models=("openai/gpt-5.6-terra", "openai/gpt-5.6-sol"),
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
    return worker, platform, gate, item


async def test_shadow_review_is_attempt_bound_and_does_not_change_verdict(
    shadow_review_case: tuple[
        ScreenerWorker, _FakePlatform, _FakeGate, ScreenerQueueItem
    ],
) -> None:
    worker, platform, _, item = shadow_review_case

    await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)

    assert len(platform.shadow_reviews) == 1
    shadow = platform.shadow_reviews[0]["request"]
    assert shadow["attempt_id"] == str(item.attempt_id)
    assert shadow["artifact_sha256"] == item.sha256
    assert shadow["settings_revision"] == 4
    assert shadow["disposition"] == "safe"
    assert len(platform.verdicts) == 1 and platform.verdicts[0]["passed"] is True


@pytest.mark.parametrize("cost, expected_cost", [(30.0, 25.0), (-1.0, 0.0)])
async def test_shadow_review_telemetry_overflow_does_not_drop_verdict(
    shadow_review_case: tuple[
        ScreenerWorker, _FakePlatform, _FakeGate, ScreenerQueueItem
    ],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cost: float,
    expected_cost: float,
) -> None:
    worker, platform, gate, item = shadow_review_case
    models = tuple(f"model-{i}" for i in range(120))
    providers = tuple(f"provider-{i}" for i in range(120))
    original = replace(
        gate.shadow_result,
        response_models=models,
        response_providers=providers,
        observation=replace(
            gate.shadow_result.observation, categories=("x" * 70,) * 10
        ),
        usage=L2Usage(estimated_cost_usd=cost, reported_cost_usd=cost),
        resolution_basis="r" * 90,
        clearance_path="c" * 110,
        critic_disposition="c" * 90,
        adjudicator_disposition="a" * 90,
    )
    gate.shadow_result = original
    claim_failure = AsyncMock()
    monkeypatch.setattr(worker, "_submit_claim_failure", claim_failure)

    with caplog.at_level("INFO"):
        await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)

    assert len(platform.shadow_reviews) == 1
    shadow = platform.shadow_reviews[0]["request"]
    assert shadow["response_models"] == list(models[-50:])
    assert shadow["response_providers"] == list(providers[-50:])
    assert shadow["categories"] == ["x" * 64] * 8
    assert shadow["usage"]["estimated_cost_usd"] == expected_cost
    assert shadow["usage"]["reported_cost_usd"] == expected_cost
    assert shadow["resolution_basis"] == "r" * 80
    assert shadow["clearance_path"] == "c" * 100
    assert shadow["critic_disposition"] == "c" * 80
    assert shadow["adjudicator_disposition"] == "a" * 80
    assert original.response_models == models
    assert original.usage.estimated_cost_usd == cost
    assert caplog.text.count("bounded shadow review telemetry") == 1
    assert "original_model_stages=120 original_provider_stages=120" in caplog.text
    assert len(platform.verdicts) == 1 and platform.verdicts[0]["passed"] is True
    assert platform.verdicts[0]["attempt_id"] == item.attempt_id
    assert platform.verdicts[0]["signature"]
    claim_failure.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    ["validation", "usage", "value", "transport", "unexpected", "call_site", "pop"],
)
async def test_shadow_review_failure_preserves_signed_verdict(
    shadow_review_case: tuple[
        ScreenerWorker, _FakePlatform, _FakeGate, ScreenerQueueItem
    ],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    worker, platform, gate, item = shadow_review_case
    claim_failure = AsyncMock()
    monkeypatch.setattr(worker, "_submit_claim_failure", claim_failure)
    private_text = "private finding or credential must not appear in logs"
    error_type = "ValueError"

    def fail_request(**_: Any) -> None:
        raise ValueError(private_text)

    if failure == "validation":
        gate.shadow_result = replace(
            gate.shadow_result,
            observation=replace(
                gate.shadow_result.observation, finding_digest=private_text
            ),
        )
        error_type = ValidationError.__name__
    elif failure == "usage":
        gate.shadow_result = replace(gate.shadow_result, usage=L2Usage(input_tokens=-1))
        error_type = ValidationError.__name__
    elif failure == "value":
        monkeypatch.setattr(
            "ditto_screener.worker.ShadowReviewObservationRequest", fail_request
        )
    elif failure == "pop":

        def fail_pop(_: UUID) -> None:
            raise RuntimeError(private_text)

        monkeypatch.setattr(gate, "pop_shadow_review", fail_pop)
        error_type = "RuntimeError"
    else:
        error = (
            PlatformError(private_text)
            if failure == "transport"
            else RuntimeError(private_text)
        )
        error_type = type(error).__name__
        if failure == "call_site":
            monkeypatch.setattr(
                worker, "_submit_shadow_review", AsyncMock(side_effect=error)
            )
        else:
            monkeypatch.setattr(
                platform, "submit_shadow_review", AsyncMock(side_effect=error)
            )

    await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)

    assert platform.shadow_reviews == []
    assert len(platform.verdicts) == 1 and platform.verdicts[0]["passed"] is True
    assert platform.verdicts[0]["attempt_id"] == item.attempt_id
    assert platform.verdicts[0]["signature"]
    claim_failure.assert_not_called()
    assert f"attempt_id={item.attempt_id}" in caplog.text
    assert f"error_type={error_type}" in caplog.text
    assert private_text not in caplog.text
    assert worker._active_attempt_id is None
    assert worker._progress_heartbeat_tasks == set()


async def test_shadow_review_not_built_in_enforce_mode(
    shadow_review_case: tuple[
        ScreenerWorker, _FakePlatform, _FakeGate, ScreenerQueueItem
    ],
    make_config: Callable[..., ScreenerConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, platform, gate, item = shadow_review_case
    worker = _worker(make_config(l2_review_mode="enforce"), platform, gate)
    worker._review_settings_status = ReviewSettingsStatus(
        revision=4,
        scope="*",
        mode="enforce",
        checksum="cd" * 32,
        source="platform",
    )
    built: list[bool] = []

    def fail_request(**_: Any) -> None:
        built.append(True)
        raise AssertionError("enforce must not construct shadow telemetry")

    monkeypatch.setattr(
        "ditto_screener.worker.ShadowReviewObservationRequest", fail_request
    )
    await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)

    assert built == []
    assert platform.shadow_reviews == []
    assert len(platform.verdicts) == 1 and platform.verdicts[0]["passed"] is True


async def test_router_source_screen_is_emitted_shadow_and_verdict_neutral(
    make_config: Callable[..., ScreenerConfig],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A built submission in shadow mode emits a benign router source screen.

    No held-out router arm producer exists yet, so the opt-in / yes-and default
    is exercised: outcome ``infrastructure``, no findings, and the signed verdict
    is untouched (still a normal PASS).
    """
    item = _item(uuid4())
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)
    worker._review_settings_status = ReviewSettingsStatus(
        revision=4,
        scope="ditto-screener-prod",
        mode="shadow",
        checksum="cd" * 32,
        source="platform",
    )
    with caplog.at_level("INFO"):
        await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)

    assert "router source screen" in caplog.text
    assert "outcome=infrastructure" in caplog.text
    # Shadow track never changes the signed verdict.
    assert len(platform.verdicts) == 1 and platform.verdicts[0]["passed"] is True


async def test_router_source_screen_is_skipped_when_not_in_shadow_mode(
    make_config: Callable[..., ScreenerConfig],
    caplog: pytest.LogCaptureFixture,
) -> None:
    item = _item(uuid4())
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)
    worker._review_settings_status = ReviewSettingsStatus(
        revision=4,
        scope="*",
        mode="enforce",
        checksum="cd" * 32,
        source="platform",
    )
    with caplog.at_level("INFO"):
        await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)

    assert "router source screen" not in caplog.text
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


async def test_v13_source_hold_uploads_image_evidence_without_passing(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    agent = uuid4()
    platform = _FakePlatform([])
    # A court refusal carrying an L1/L2 finding, as the real policy emits it.
    finding = SourceReviewFinding(
        artifact_sha256="de" * 32,
        prompt_revision="source-review-v2",
        risk_level="high",
        confidence=0.97,
        categories=["cross_user_access"],
        summary="Unverified cross-user lead held for the court.",
    )
    decision = PolicyEngine(CORE_ONLY_MANIFEST).preexecution_source_decision(
        SourceReviewObservation(
            ok=False,
            risk_level=None,
            finding_digest=finding.canonical_digest(),
            categories=("cross_user_access",),
            failure_disposition="inconclusive",
            finding=finding.model_dump(mode="json"),
            adjudication=SourceReviewAdjudication(
                decision="escalate",
                reason="the court timed out before a verified finding",
                escalation_code="adjudicator-failed",
                model="z-ai/glm-5.3-flash",
                prompt_revision="adjudicator-v7-policy-v13",
            ).model_dump(mode="json"),
        ),
        policy_version=13,
    )
    assert decision.outcome == ScreeningOutcome.QUARANTINE
    assert decision.finding is not None
    worker = _worker(make_config(), platform, _FakeGate(decision))

    await worker._screen_one(_item(agent), policy_version=13)

    assert len(platform.image_uploads) == 1
    assert [r["check_code"] for r in platform.verification_receipts] == [
        "archive_sha",
        "build_image_digest",
    ]
    verdict = platform.verdicts[0]
    assert verdict["outcome"] == ScreenResultOutcome.QUARANTINE
    assert verdict["passed"] is False
    assert verdict["image_sha256"] is None
    assert verdict["image_upload_id"] is None


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


async def test_build_only_quarantine_is_rejected_as_retryable_worker_failure(
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
    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    assert verdict["outcome"] == ScreenResultOutcome.RETRYABLE_INFRA
    assert verdict["reason_code"] == "worker-platform-request-failed"
    assert "build-only screen produced a quarantine" in verdict["detail"]


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


async def test_legacy_source_budget_exhaustion_is_signed_and_submitted_once(
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
        review_notes=(
            {
                "kind": "observation",
                "category": "review_budget",
                "summary": "Collected bounded review evidence before exhaustion.",
                "stage": "l1",
            },
        ),
    )
    platform = _FakePlatform([])
    worker = _worker(make_config(), platform, _FakeGate(result))

    await worker._screen_one(_item(uuid4()), policy_version=12)

    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    assert verdict["passed"] is True
    assert verdict["outcome"] == ScreenResultOutcome.PASS_INCONCLUSIVE
    assert verdict["review_audit_digest"] == audit.canonical_digest()
    expected_notes = [
        SourceReviewNote(
            kind="observation",
            category="review_budget",
            summary="Collected bounded review evidence before exhaustion.",
            stage="l1",
        )
    ]
    assert verdict["review_notes"] == expected_notes
    assert verdict["review_notes_digest"] == source_review_notes_digest(expected_notes)
    assert verdict["reason_code"] == "source-review-inconclusive"


async def test_v13_source_budget_exhaustion_is_nonpassing_with_signed_evidence(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    audit = ScreenReviewAudit(
        stage="l1",
        reason_code="source-review-step-budget-exhausted",
        prompt_revision="source-review-v24-policy-v13",
        max_steps=20,
        steps_used=20,
        max_read_bytes=2_000_000,
        read_bytes_used=123_456,
    )
    result = ScreeningDecision(
        outcome=ScreeningOutcome.INCONCLUSIVE,
        detail="bounded source review inconclusive; retry or deadline required",
        manifest_digest="ab" * 32,
        evidence=(
            PolicyEvidence(
                module_id="luna-source-review",
                code="source-review-inconclusive",
                summary="bounded source review exhausted without a decisive finding",
            ),
        ),
        review_audit=audit.model_dump(mode="json"),
        review_notes=(
            {
                "kind": "observation",
                "category": "review_budget",
                "summary": "Collected bounded review evidence before exhaustion.",
                "stage": "l1",
            },
        ),
    )
    platform = _FakePlatform([])
    worker = _worker(make_config(), platform, _FakeGate(result))

    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)

    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    assert verdict["passed"] is False
    assert verdict["outcome"] == ScreenResultOutcome.INCONCLUSIVE
    assert verdict["manifest_digest"] == "ab" * 32
    assert verdict["review_audit"] == audit
    assert verdict["review_audit_digest"] == audit.canonical_digest()
    assert verdict["evidence"] is not None
    assert verdict["reason_code"] == "source-review-inconclusive"


async def test_worker_refuses_to_sign_a_mismatched_decision_policy(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    result = core_decision(
        ScreeningOutcome.INCONCLUSIVE,
        code="source-review-inconclusive",
        summary="review did not complete",
        detail="private policy audit inconclusive",
        policy_version=SCREENING_POLICY_VERSION,
    )
    platform = _FakePlatform([])
    gate = _FakeGate(result, bind_policy_version=False)
    worker = _worker(make_config(), platform, gate)

    await worker._screen_one(_item(uuid4()), policy_version=12)

    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    assert verdict["passed"] is False
    assert verdict["policy_version"] == 12
    assert verdict["outcome"] == ScreenResultOutcome.RETRYABLE_INFRA
    assert verdict["reason_code"] == "worker-platform-request-failed"
    assert "decision policy version does not match" in verdict["detail"]


async def test_passing_source_review_notes_keep_the_policy_manifest_binding(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    result = replace(
        _decision(ScreeningOutcome.PASS),
        review_notes=(
            {
                "kind": "cleared",
                "category": "general_runtime",
                "summary": "Reviewed the normal provider-bound execution path.",
                "stage": "l1",
            },
        ),
    )
    platform = _FakePlatform([])
    worker = _worker(make_config(), platform, _FakeGate(result))

    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)

    verdict = platform.verdicts[0]
    assert verdict["manifest_digest"] == result.manifest_digest
    assert verdict["review_notes"] is not None


async def test_passing_gate_without_verified_image_posts_retryable_worker_failure(
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
    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    assert verdict["outcome"] == ScreenResultOutcome.RETRYABLE_INFRA
    assert verdict["reason_code"] == "worker-platform-request-failed"
    assert "passing screen did not publish a prebuilt image" in verdict["detail"]


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


@pytest.mark.parametrize("reason_code", ["docker-build", "docker-build-timeout"])
async def test_local_build_failure_forwards_signed_private_miner_feedback(
    make_config: Callable[..., ScreenerConfig],
    reason_code: str,
) -> None:
    platform = _FakePlatform([])
    gate = _FakeGate(
        core_decision(
            ScreeningOutcome.DETERMINISTIC_REJECT,
            code=reason_code,
            summary="artifact Docker image did not build",
            detail="build failed: token=secret-value\nerror: missing Cargo.toml",
        )
    )
    worker = _worker(make_config(), platform, gate)

    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)

    verdict = platform.verdicts[0]
    assert verdict["private_failure_detail"] is not None
    assert verdict["private_failure_log_tail"] is not None
    assert "secret-value" not in verdict["private_failure_detail"]
    assert "[REDACTED]" in verdict["private_failure_log_tail"]
    _signed_request(verdict)


async def test_seed_probe_rejection_forwards_private_miner_feedback(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    # The public reason is a fixed category, so the actionable part has to
    # reach the submission owner through the private channel or #411's point
    # is lost: the miner is told screening failed and nothing more.
    platform = _FakePlatform([])
    gate = _FakeGate(
        core_decision(
            ScreeningOutcome.DETERMINISTIC_REJECT,
            code="seed-readonly-write",
            summary="container did not satisfy the seeding contract",
            detail=(
                "serve check failed: /seed failed writing outside the sandbox's "
                "writable filesystem. token=secret-value"
            ),
        )
    )
    worker = _worker(make_config(), platform, gate)

    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)

    verdict = platform.verdicts[0]
    assert verdict["reason_code"] == "seed-readonly-write"
    assert verdict["private_failure_detail"] is not None
    assert "/seed" in verdict["private_failure_detail"]
    assert "secret-value" not in verdict["private_failure_detail"]
    _signed_request(verdict)


def _shadow_seed_evidence(decision: ScreeningDecision) -> ScreeningDecision:
    """Append the records shadow mode adds without changing the outcome."""
    return replace(
        decision,
        evidence=(
            *decision.evidence,
            PolicyEvidence(
                "stable-core",
                "seed-readonly-write",
                "shadow seed probe observed a read-only filesystem write",
            ),
            PolicyEvidence(
                "stable-core",
                "seed-envelope-usage",
                "memory peak 120 MiB of 3072 MiB",
            ),
        ),
    )


def _signed_request(verdict: dict[str, Any]) -> ScreenResultRequest:
    return ScreenResultRequest(
        screener_hotkey=_MINER,
        **{key: value for key, value in verdict.items() if key != "agent_id"},
    )


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (ScreenResultOutcome.PASS, "health-ok"),
        (ScreenResultOutcome.PASS_INCONCLUSIVE, "source-review-inconclusive"),
        (ScreenResultOutcome.QUARANTINE, "source-finding-held"),
        (ScreenResultOutcome.INCONCLUSIVE, "source-review-inconclusive"),
        (ScreenResultOutcome.RETRYABLE_INFRA, "source-review-unavailable"),
    ],
)
def test_legacy_reason_ignores_shadow_seed_for_every_non_rejection(
    outcome: ScreenResultOutcome, reason: str
) -> None:
    evidence = (
        PolicyEvidence("review", reason, "deciding result"),
        PolicyEvidence("stable-core", "seed-http-error", "shadow seed failed"),
        PolicyEvidence("stable-core", "seed-envelope-usage", "sandbox headroom"),
    )
    assert _verdict_reason_code(outcome, evidence) == reason


@pytest.mark.parametrize(
    "outcome", [ScreenResultOutcome.INCONCLUSIVE, ScreenResultOutcome.QUARANTINE]
)
def test_legacy_reason_ignores_trailing_successful_challenge_observations(
    outcome: ScreenResultOutcome,
) -> None:
    evidence = (
        PolicyEvidence("review", "source-review-inconclusive", "deciding result"),
        PolicyEvidence("oracle", "behavioral-oracle-passed", "oracle passed"),
        PolicyEvidence("pack", "challenge-observed", "challenge completed"),
    )
    assert _verdict_reason_code(outcome, evidence) == "source-review-inconclusive"


def test_legacy_reason_does_not_reintroduce_a_filtered_seed_observation() -> None:
    evidence = (PolicyEvidence("stable-core", "seed-http-error", "shadow seed failed"),)
    assert _verdict_reason_code(ScreenResultOutcome.RETRYABLE_INFRA, evidence) is None
    assert (
        _verdict_reason_code(ScreenResultOutcome.DETERMINISTIC_REJECT, evidence)
        == "seed-http-error"
    )


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (ScreeningOutcome.INCONCLUSIVE, "source-review-inconclusive"),
        (ScreeningOutcome.RETRYABLE_INFRA, "source-review-unavailable"),
        (ScreeningOutcome.QUARANTINE, "source-finding-held"),
    ],
)
async def test_worker_prefers_the_deciding_reason_in_signed_verdicts(
    make_config: Callable[..., ScreenerConfig], outcome: ScreeningOutcome, reason: str
) -> None:
    decision = core_decision(
        outcome,
        code=reason,
        summary="source review did not resolve the submission",
        detail="private policy review did not complete",
    )
    decision = _shadow_seed_evidence(decision)
    decision = replace(
        decision,
        evidence=(
            *decision.evidence,
            PolicyEvidence("another-module", "module-cleared", "module cleared"),
        ),
    )
    platform = _FakePlatform([])
    worker = _worker(make_config(), platform, _FakeGate(decision))
    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)
    assert len(platform.verdicts) == 1
    request = _signed_request(platform.verdicts[0])
    assert request.outcome == ScreenResultOutcome(outcome.value)
    assert request.reason_code == reason
    if outcome != ScreeningOutcome.RETRYABLE_INFRA:
        assert request.evidence is not None
        assert request.evidence[-1].code == "module-cleared"


async def test_group_readable_court_key_submits_the_fleet_retry_code(
    make_config: Callable[..., ScreenerConfig], tmp_path: Any
) -> None:
    """A 0644 key on a node parks nothing: Platform retries the exact code."""
    key = tmp_path / "adjudicator.key"
    key.write_text("sk-test-private-adjudicator")
    os.chmod(key, 0o644)
    adjudication = await SourceReviewAdjudicator(
        api_key_file=str(key), base_url="https://openrouter.test/api/v1"
    ).adjudicate(str(tmp_path / "never-opened.tar.gz"), notes=[])
    observation = SourceReviewObservation(
        ok=False,
        risk_level=None,
        finding_digest=None,
        categories=(),
        error_code="source-review-oserror",
        failure_disposition="retryable_infra",
        adjudication=adjudication.model_dump(mode="json"),
    )
    decision = PolicyEngine(CORE_ONLY_MANIFEST).preexecution_source_decision(
        observation
    )
    platform = _FakePlatform([])
    worker = _worker(make_config(), platform, _FakeGate(decision))

    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)

    assert len(platform.verdicts) == 1
    request = _signed_request(platform.verdicts[0])
    assert request.outcome == ScreenResultOutcome.RETRYABLE_INFRA
    assert request.reason_code == SOURCE_REVIEW_KEY_UNAVAILABLE_CODE


async def test_shadow_seed_observation_keeps_quarantine_verdict_signed(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    # Shadow /seed evidence is appended after the deciding code. Signing it as
    # private failure feedback makes ScreenResultRequest reject a quarantine
    # with "private failure feedback requires a failure outcome", and the
    # worker then parks the attempt as worker-result-processing-failed.
    platform = _FakePlatform([])
    gate = _FakeGate(
        _shadow_seed_evidence(
            core_decision(
                ScreeningOutcome.QUARANTINE,
                code="benchmark-emulation",
                summary="source review held the submission for operator review",
                detail="private policy quarantine pending operator review",
            )
        )
    )
    worker = _worker(make_config(), platform, gate)

    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)

    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    request = _signed_request(verdict)
    assert request.outcome == ScreenResultOutcome.QUARANTINE
    assert request.passed is False
    assert request.reason_code == "benchmark-emulation"
    assert request.private_failure_detail is None
    assert request.private_failure_log_tail is None
    assert [item.code for item in request.evidence or []] == [
        "benchmark-emulation",
        "seed-readonly-write",
        "seed-envelope-usage",
    ]


async def test_v13_court_clear_is_signed_as_protocol_valid_quarantine(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    # Platform refuses a v13 court clear carried by PASS. The worker must sign
    # the held quarantine shape that the shared protocol and Platform accept.
    adjudication = SourceReviewAdjudication(
        decision="clear",
        reason="The served model authors the graded response",
        clear_clause="model_authors_graded_slot",
        citations=[SourceReviewCitation(path="src/main.rs", line=6)],
        model="z-ai/glm-5.3-flash",
        prompt_revision="adjudicator-v7-policy-v13",
        completion_receipt=AdjudicationCompletionReceipt(
            elapsed_ms=4300,
            observed_model="z-ai/glm-5.3-flash",
            request_count=1,
        ),
    ).model_dump(mode="json")
    decision = PolicyEngine(CORE_ONLY_MANIFEST).preexecution_source_decision(
        SourceReviewObservation(
            ok=False,
            risk_level=None,
            finding_digest=None,
            categories=(),
            error_code="l3-adjudicator-incomplete",
            failure_disposition="retryable_infra",
            adjudication=adjudication,
        ),
        policy_version=13,
    )
    platform = _FakePlatform([])
    worker = _worker(make_config(), platform, _FakeGate(decision))

    await worker._screen_one(
        _item(uuid4(), policy_version=13),
        policy_version=13,
        normal_review_settings=platform.review_settings.model_copy(
            update={"revision": 7, "scope": "*"}
        ),
    )

    request = _signed_request(platform.verdicts[0])
    assert request.outcome == ScreenResultOutcome.QUARANTINE
    assert request.passed is False
    assert request.reason_code == "source-review-awaiting-v13-verification"
    assert request.adjudication is not None
    assert request.adjudication.decision == "clear"
    assert request.completion_receipt_signature is not None


async def test_shadow_seed_observation_keeps_pass_verdict_signed(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    gate = _FakeGate(
        _shadow_seed_evidence(
            core_decision(
                ScreeningOutcome.PASS,
                code="health-ok",
                summary="container satisfied the health gate",
                detail="",
            )
        )
    )
    worker = _worker(make_config(), platform, gate)

    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)

    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    request = _signed_request(verdict)
    assert request.outcome == ScreenResultOutcome.PASS
    assert request.passed is True
    assert request.reason_code == "health-ok"
    assert request.private_failure_detail is None
    assert request.private_failure_log_tail is None
    assert request.evidence is None


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
    assert platform.verdicts[0]["private_failure_detail"] is not None
    assert platform.verdicts[0]["private_failure_log_tail"] is not None


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


async def test_inconclusive_challenge_keeps_safe_http_status_for_miner_feedback(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    gate = _FakeGate(
        ScreeningDecision(
            outcome=ScreeningOutcome.INCONCLUSIVE,
            detail="private policy audit inconclusive",
            manifest_digest="ab" * 32,
            evidence=(
                PolicyEvidence(
                    "v8-behavioral-oracle",
                    "challenge-http-422",
                    "always-on behavioral oracle did not produce a usable result",
                ),
            ),
        )
    )
    worker = _worker(make_config(), platform, gate)

    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)

    verdict = platform.verdicts[0]
    assert verdict["private_failure_detail"] is not None
    assert "HTTP 422" in verdict["private_failure_detail"]
    assert verdict["private_failure_log_tail"] == verdict["private_failure_detail"]


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
    # Budgets carved from the lease (e.g. L1/L2 before the court reserve)
    # must observe the same renewal.
    derived = worker._active_lease_deadline.offset(30)

    await worker._report_heartbeat("screening", force=True)

    assert worker._active_lease_deadline.expires_at > (
        asyncio.get_running_loop().time() + 9 * 60
    )
    assert derived.expires_at == worker._active_lease_deadline.expires_at - 30


async def test_same_stage_heartbeat_follows_platform_lease_renewal(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    worker = _worker(
        make_config(), platform, _FakeGate(_decision(ScreeningOutcome.PASS))
    )
    worker._active_agent_id = uuid4()
    worker._active_progress_stage = "source_review_60"
    worker._job_started_at = int(datetime.now(UTC).timestamp())
    worker._active_lease_deadline = LeaseDeadline(asyncio.get_running_loop().time() + 1)
    for minutes in (10, 20):
        renewed = datetime.now(UTC) + timedelta(minutes=minutes)
        platform.heartbeat_lease_deadline = renewed

        await worker._report_heartbeat("screening", force=True)

        assert worker._active_progress_stage == "source_review_60"
        assert worker._active_lease_wall == renewed
        assert worker._active_lease_deadline.expires_at > (
            asyncio.get_running_loop().time() + (minutes - 1) * 60
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


@pytest.mark.parametrize("status_code", [400, 413, 422, None])
@pytest.mark.parametrize("fallback_fails", [False, True])
async def test_definitive_verdict_failure_submits_one_attempt_bound_fallback(
    make_config: Callable[..., ScreenerConfig],
    status_code: int | None,
    fallback_fails: bool,
) -> None:
    platform = _FakePlatform([])
    worker = _worker(
        make_config(), platform, _FakeGate(_decision(ScreeningOutcome.PASS))
    )
    item = _item(uuid4())
    error = (
        PlatformRejected(status_code=status_code, body="invalid signed review audit")
        if status_code is not None
        else PlatformAuthOnlyFailure("credential refresh failed")
    )
    original_submit = platform.submit_result
    calls: list[dict[str, Any]] = []

    async def submit(agent_id: UUID, **kwargs: Any):
        calls.append({"agent_id": agent_id, **kwargs})
        if len(calls) == 1:
            raise error
        if fallback_fails:
            raise PlatformRejected(status_code=409, body="attempt already completed")
        return await original_submit(agent_id, **kwargs)

    platform.submit_result = submit  # type: ignore[method-assign]
    await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)

    assert len(calls) == 2
    verdict = calls[1]
    request = _signed_request(verdict)
    assert verdict["agent_id"] == item.agent_id
    assert request.attempt_id == item.attempt_id
    assert request.outcome == ScreenResultOutcome.RETRYABLE_INFRA
    assert request.passed is False
    assert request.reason_code == (
        "worker-verdict-rejected"
        if status_code is not None
        else "worker-verdict-auth-failed"
    )
    assert request.private_failure_detail is not None
    if status_code is not None:
        assert str(status_code) in request.private_failure_detail
        assert "invalid signed review audit" in request.private_failure_detail
    else:
        assert "no verdict request was sent" in request.private_failure_detail
    assert worker._active_attempt_id is None
    assert worker._active_agent_id is None


@pytest.mark.parametrize(
    "error",
    [
        PlatformError("verdict submit failed: response lost"),
        PlatformError("verdict rejected (503): unavailable"),
        PlatformRejected(status_code=401, body="unauthorized"),
        PlatformRejected(status_code=409, body="agent no longer screenable"),
    ],
)
async def test_ambiguous_or_unauthorized_verdict_failure_never_submits_fallback(
    make_config: Callable[..., ScreenerConfig], error: PlatformError
) -> None:
    platform = _FakePlatform([])
    platform.submit_result = AsyncMock(side_effect=error)
    worker = _worker(
        make_config(), platform, _FakeGate(_decision(ScreeningOutcome.PASS))
    )
    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)
    assert platform.submit_result.await_count == 1
    assert platform.verdicts == []


async def test_accepted_verdict_with_lost_response_then_conflict_has_no_fallback(
    make_config: Callable[..., ScreenerConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conflicting retry must never replace the original signed result."""
    cfg = make_config()
    platform = _FakePlatform([])
    worker = _worker(cfg, platform, _FakeGate(_decision(ScreeningOutcome.PASS)))
    item = _item(uuid4())
    requests: list[httpx.Request] = []
    accepted: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            # Platform commits the pass, but the connection loses its response.
            accepted.update(json.loads(request.content))
            raise httpx.ReadError("accepted response lost", request=request)
        # The agent changed state before the idempotent retry arrived. The
        # same 409 would also refuse any replacement infrastructure verdict.
        return httpx.Response(409, text="agent no longer screenable")

    monkeypatch.setattr(
        "ditto_screener.platform._TRANSIENT_PLATFORM_RETRY_DELAYS", (0.0,)
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlatformClient(cfg, http)
        platform.submit_result = client.submit_result  # type: ignore[method-assign]
        await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)

    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert accepted["attempt_id"] == str(item.attempt_id)
    assert accepted["outcome"] == ScreenResultOutcome.PASS.value
    assert accepted["passed"] is True
    assert worker._active_attempt_id is None
    assert worker._active_agent_id is None
    assert platform.heartbeats[-1].state == "polling"


async def test_pre_verdict_platform_error_posts_retryable_failure(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    agent_id = uuid4()
    platform = _FakePlatform([])
    platform.artifact_error = PlatformError("artifact rejected (503): unavailable")
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)
    item = _item(agent_id)

    await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)

    assert gate.calls == []
    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    assert verdict["agent_id"] == agent_id
    assert verdict["attempt_id"] == item.attempt_id
    assert verdict["outcome"] == ScreenResultOutcome.RETRYABLE_INFRA
    assert verdict["reason_code"] == "worker-platform-request-failed"
    assert "artifact rejected (503)" in verdict["private_failure_detail"]
    ScreenResultRequest(
        screener_hotkey=_MINER,
        **{key: value for key, value in verdict.items() if key != "agent_id"},
    )
    assert worker._active_agent_id is None
    assert platform.heartbeats[-1].state == "polling"


async def test_unexpected_worker_error_posts_retryable_failure_without_escaping(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    agent_id = uuid4()
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))

    async def crash(**_kwargs: Any) -> ScreeningDecision:
        raise ValueError("malformed worker result")

    gate.screen = crash  # type: ignore[method-assign]
    worker = _worker(make_config(), platform, gate)

    await worker._screen_one(_item(agent_id), policy_version=SCREENING_POLICY_VERSION)

    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    assert verdict["outcome"] == ScreenResultOutcome.RETRYABLE_INFRA
    assert verdict["reason_code"] == "worker-result-processing-failed"
    assert "malformed worker result" in verdict["private_failure_log_tail"]
    assert worker._active_agent_id is None
    assert platform.heartbeats[-1].state == "polling"


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


async def test_screen_one_passes_claim_receipt_time_to_gate(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)
    await worker._screen_one(
        _item(uuid4()), policy_version=SCREENING_POLICY_VERSION, received_at=1234
    )
    before = int(time.time())
    await worker._screen_one(_item(uuid4()), policy_version=SCREENING_POLICY_VERSION)
    assert gate.received_at[0] == 1234
    received = gate.received_at[1]
    assert received is not None and before <= received <= int(time.time())


async def test_every_item_of_one_claim_shares_its_receipt_time(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    """A later item's signed lease is judged from the claim, not its own start."""
    first, second = uuid4(), uuid4()
    platform = _FakePlatform([[_item(first), _item(second)]])
    stop = asyncio.Event()
    platform.stop_after_queue = stop
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    original = gate.screen

    async def slow_screen(*args, **kwargs):  # type: ignore[no-untyped-def]
        if not gate.calls:
            await asyncio.sleep(1.1)
        return await original(*args, **kwargs)

    gate.screen = slow_screen  # type: ignore[method-assign]
    before = int(time.time())
    worker = _worker(make_config(), platform, gate)
    await asyncio.wait_for(worker.run_forever(stop), timeout=5.0)
    assert gate.calls == [first, second]
    assert gate.received_at[0] == gate.received_at[1]
    received = gate.received_at[0]
    assert received is not None and before <= received < int(time.time())


async def test_stop_during_review_finishes_the_signed_verdict(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    first, second = uuid4(), uuid4()
    platform = _FakePlatform([[_item(first), _item(second)]])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    stop = asyncio.Event()
    original = gate.screen

    async def screen(*args, **kwargs):  # type: ignore[no-untyped-def]
        stop.set()
        return await original(*args, **kwargs)

    gate.screen = screen  # type: ignore[method-assign]
    worker = _worker(make_config(), platform, gate)
    await asyncio.wait_for(worker.run_forever(stop), timeout=2.0)
    assert gate.calls == [first]
    assert [verdict["agent_id"] for verdict in platform.verdicts] == [first, second]
    assert platform.verdicts[0]["passed"] is True
    # The second lease was durable but never started: settle it, do not drop it.
    _assert_claim_not_started(platform.verdicts[1], second)


def _assert_claim_not_started(verdict: dict[str, Any], agent_id: UUID) -> None:
    assert verdict["agent_id"] == agent_id
    assert verdict["passed"] is False
    assert verdict["outcome"] == ScreenResultOutcome.RETRYABLE_INFRA
    assert verdict["reason_code"] == "worker-claim-not-started"
    ScreenResultRequest(
        screener_hotkey=_MINER,
        **{key: value for key, value in verdict.items() if key != "agent_id"},
    )


async def test_no_claim_when_stopped_before_claim(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    """A drain SIGTERM during the pre-claim awaits must not take a lease."""
    platform = _FakePlatform([[_item(uuid4())]])
    stop = asyncio.Event()
    original = platform.get_required_policy_version

    async def required_policy() -> int:
        stop.set()
        return await original()

    platform.get_required_policy_version = required_policy  # type: ignore[method-assign]
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)

    assert await worker._sweep(stop) == 0
    assert platform.claim_calls == 0
    assert platform.verdicts == []
    assert gate.calls == []


async def test_credits_exhausted_pauses_claims_then_probes(
    make_config: Callable[..., ScreenerConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 402 stops claiming so a funding gap cannot fail the whole queue."""
    clock = [1000.0]
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: clock[0])
    platform = _FakePlatform([[_item(uuid4())], [_item(uuid4())]])
    worker = _worker(
        make_config(), platform, _FakeGate(_decision(ScreeningOutcome.PASS))
    )
    stop = asyncio.Event()

    worker._observe_credits(SOURCE_REVIEW_PROVIDER_CREDITS_EXHAUSTED)
    assert await worker._sweep(stop) == 0
    assert platform.claim_calls == 0

    clock[0] += worker_module.CREDITS_PAUSE_INITIAL_SECONDS + 1
    assert await worker._sweep(stop) == 1
    assert platform.claim_calls == 1


def test_credits_pause_doubles_to_cap_and_clears_on_any_other_outcome(
    make_config: Callable[..., ScreenerConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: 0.0)
    worker = _worker(
        make_config(), _FakePlatform([]), _FakeGate(_decision(ScreeningOutcome.PASS))
    )
    for expected in (300.0, 600.0, 1200.0, 1800.0, 1800.0):
        worker._observe_credits(SOURCE_REVIEW_PROVIDER_CREDITS_EXHAUSTED)
        assert worker._credits_pause_seconds == expected
    worker._observe_credits("source-review-http-401")
    assert worker._credits_pause_seconds == 0.0
    assert not worker._credits_paused()


async def test_stop_set_during_claim_still_screens_claimed_item(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    agent_id = uuid4()
    item = _item(agent_id)
    platform = _FakePlatform([[item]])
    stop = asyncio.Event()
    original = platform.claim_next

    async def claim_next(**kwargs: Any) -> ScreenerQueueResponse:
        stop.set()
        return await original(**kwargs)

    platform.claim_next = claim_next  # type: ignore[method-assign]
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)

    assert await worker._sweep(stop) == 1
    assert gate.calls == [agent_id]
    assert len(platform.verdicts) == 1
    assert platform.verdicts[0]["attempt_id"] == item.attempt_id
    assert platform.verdicts[0]["passed"] is True


async def test_policy_change_during_claim_fails_claimed_items_explicitly(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    agent_id = uuid4()
    item = _item(agent_id, policy_version=SCREENING_FLOOR_POLICY_VERSION)
    platform = _FakePlatform([])
    platform.required_policy_version = SCREENING_FLOOR_POLICY_VERSION

    async def claim_next(**_: Any) -> ScreenerQueueResponse:
        platform.claim_calls += 1
        return ScreenerQueueResponse(
            items=[item],
            count=1,
            required_policy_version=SCREENING_FLOOR_POLICY_VERSION + 1,
        )

    platform.claim_next = claim_next  # type: ignore[method-assign]
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)

    assert await worker._sweep(asyncio.Event()) == 0
    assert gate.calls == []
    assert platform.artifact_calls == []
    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    _assert_claim_not_started(verdict, agent_id)
    assert verdict["attempt_id"] == item.attempt_id
    # Platform binds the attempt to its claimed policy and accepts no other.
    assert verdict["policy_version"] == SCREENING_FLOOR_POLICY_VERSION
    assert (
        "changed screening policy during claim" in (verdict["private_failure_detail"])
    )


async def test_out_of_range_item_policy_is_failed_not_dropped(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    agent_id = uuid4()
    item = _item(agent_id, policy_version=SCREENING_POLICY_VERSION + 1)
    platform = _FakePlatform([[item]])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)

    assert await worker._sweep(asyncio.Event()) == 0
    assert gate.calls == []
    assert platform.artifact_calls == []
    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    _assert_claim_not_started(verdict, agent_id)
    assert verdict["attempt_id"] == item.attempt_id
    assert verdict["policy_version"] == SCREENING_POLICY_VERSION + 1
    assert (
        "outside this worker's supported range" in (verdict["private_failure_detail"])
    )


async def test_invalid_claim_response_fails_recoverable_attempts(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    from ditto_screener.platform import ClaimedAttemptRef, ClaimResponseInvalid

    agent_id, attempt_id = uuid4(), uuid4()
    platform = _FakePlatform([])

    async def claim_next(**_: Any) -> ScreenerQueueResponse:
        platform.claim_calls += 1
        raise ClaimResponseInvalid(
            "screening claim response invalid: items.0.name: Field required",
            (
                ClaimedAttemptRef(
                    agent_id=agent_id,
                    attempt_id=attempt_id,
                    policy_version=SCREENING_POLICY_VERSION,
                ),
                ClaimedAttemptRef(agent_id=uuid4()),
            ),
        )

    platform.claim_next = claim_next  # type: ignore[method-assign]
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)

    assert await worker._sweep(asyncio.Event()) == 0
    assert gate.calls == []
    # The ref without an attempt id has nothing to sign against.
    assert len(platform.verdicts) == 1
    verdict = platform.verdicts[0]
    _assert_claim_not_started(verdict, agent_id)
    assert verdict["attempt_id"] == attempt_id
    assert verdict["policy_version"] == SCREENING_POLICY_VERSION
    assert "claim response invalid" in verdict["private_failure_detail"]


async def test_local_drain_lease_follows_heartbeat_renewal_and_clears(
    make_config: Callable[..., ScreenerConfig], tmp_path: Any
) -> None:
    """The updater's lease view tracks Platform renewals, then disappears.

    Renewable leases are 10 minutes. Without following the renewal a live
    review would look expired to the release drain after one TTL.
    """
    journal = tmp_path / "workers" / "1" / "review.jsonl"
    lease = journal.with_name("active-lease.json")
    platform = _FakePlatform([])
    initial = datetime.now(UTC) + timedelta(minutes=10)
    renewed = initial + timedelta(minutes=30)
    platform.heartbeat_lease_deadline = renewed
    seen: list[dict[str, Any]] = []
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(review_journal_file=str(journal)), platform, gate)
    original = gate.screen

    async def screen(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(json.loads(lease.read_text()))
        await worker._report_heartbeat("screening", force=True)
        seen.append(json.loads(lease.read_text()))
        return await original(*args, **kwargs)

    gate.screen = screen  # type: ignore[method-assign]
    item = _item(uuid4(), lease_deadline=initial)
    await worker._screen_one(item, policy_version=SCREENING_POLICY_VERSION)

    assert seen[0]["lease_deadline"] == int(initial.timestamp())
    assert seen[0]["attempt_id"] == str(item.attempt_id)
    assert seen[1]["lease_deadline"] == int(renewed.timestamp())
    assert not lease.exists()


async def test_unwritable_drain_lease_never_aborts_a_review(
    make_config: Callable[..., ScreenerConfig], tmp_path: Any
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    platform = _FakePlatform([])
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(
        make_config(review_journal_file=str(blocker / "1" / "review.jsonl")),
        platform,
        gate,
    )
    agent = uuid4()
    await worker._screen_one(_item(agent), policy_version=SCREENING_POLICY_VERSION)
    assert [verdict["agent_id"] for verdict in platform.verdicts] == [agent]


async def test_worker_start_clears_a_lease_left_by_a_dead_process(
    make_config: Callable[..., ScreenerConfig], tmp_path: Any
) -> None:
    journal = tmp_path / "review.jsonl"
    lease = journal.with_name("active-lease.json")
    lease.write_text('{"lease_deadline": 1, "progress_at": 1}')
    stop = asyncio.Event()
    stop.set()
    worker = _worker(
        make_config(review_journal_file=str(journal)),
        _FakePlatform([]),
        _FakeGate(_decision(ScreeningOutcome.PASS)),
    )
    await worker.run_forever(stop)
    assert not lease.exists()


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
    platform.required_policy_version = SCREENING_FLOOR_POLICY_VERSION - 1
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
    platform = _FakePlatform(
        [[_item(uuid4(), policy_version=SCREENING_FLOOR_POLICY_VERSION)]]
    )
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


async def test_isolated_canary_uses_item_policy_without_changing_fleet_heartbeat(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    """A V11 canary stays claimable while the ordinary queue remains at V10."""
    platform = _FakePlatform(
        [[_item(uuid4(), policy_version=SCREENING_POLICY_VERSION)]]
    )
    platform.required_policy_version = SCREENING_FLOOR_POLICY_VERSION
    gate = _FakeGate(_decision(ScreeningOutcome.PASS))
    worker = _worker(make_config(), platform, gate)

    assert await worker._sweep(asyncio.Event()) == 1
    assert platform.claimed_policy_versions == [SCREENING_FLOOR_POLICY_VERSION]
    assert gate.policy_versions == [SCREENING_POLICY_VERSION]
    assert platform.verdicts[0]["policy_version"] == SCREENING_POLICY_VERSION
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
