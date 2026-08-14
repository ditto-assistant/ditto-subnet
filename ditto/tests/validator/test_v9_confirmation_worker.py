"""Worker-level invariants for the private Bench v9 confirmation lane.

Transport tests cover HTTP and signing bytes.  These tests cover orchestration:
canonical work gets first refusal, readiness is fail-closed, each spare slot is
isolated, and no outcome can escape into either production score endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from ditto.api_models.validator import ArtifactResponse
from ditto.api_models.validator_confirmation import (
    ConfirmationBundleMode,
    ConfirmationExecutionProfile,
    V9ConfirmationJobResponse,
    V9ConfirmationPreparedReport,
    V9ConfirmationScorerReadiness,
    V9ConfirmationScorerResult,
)
from ditto.validator import worker as worker_mod
from ditto.validator.dittobench import InferenceBrokerSession
from ditto.validator.errors import (
    DittobenchError,
    PlatformError,
    ValidatorInfrastructureError,
)
from ditto.validator.worker import ValidatorWorker

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_PROFILE_REVISION = "confirmation-v9-calibrated-1"
_PROFILE_CHECKSUM = "11" * 32
_ARTIFACT_SHA = "a" * 64
_BROKER_PUBLIC_KEY = "A" * 43


class _RecordingKeypair:
    def __init__(self) -> None:
        self.messages: list[bytes] = []

    def sign(self, message: bytes) -> bytes:
        self.messages.append(message)
        return hashlib.sha512(message).digest()


def _config(*, capacity: int = 2) -> Any:
    return cast(
        Any,
        SimpleNamespace(
            validator_hotkey=_HOTKEY,
            netuid=118,
            benchmark_capacity=capacity,
            longmem_capacity=(capacity + 1) // 2,
            sweep_seconds=30,
            queue_limit=16,
            burn_hotkey="5Burn" + "x" * 43,
        ),
    )


def _profile(
    *,
    revision: str = _PROFILE_REVISION,
    checksum: str = _PROFILE_CHECKSUM,
) -> ConfirmationExecutionProfile:
    budget = {
        "max_chat_requests": 20,
        "max_chat_input_bytes": 20_000,
        "max_embedding_requests": 10,
        "max_embedding_inputs": 20,
        "max_embedding_input_bytes": 10_000,
    }
    return ConfirmationExecutionProfile.model_validate(
        {
            "schema_version": 1,
            "revision": revision,
            "checksum": checksum,
            "longmem_profile_revision": "longmem-profile-v1",
            "longmem_profile_checksum": "44" * 32,
            "longmem_dataset_revision": "longmem-pinned-v1",
            "longmem_dataset_sha256": "55" * 32,
            "longmem_selector_revision": "longmemeval-s-stratified-sha256-v1",
            "longmem_selection_seed": 118,
            "longmem_cases_per_capability": 2,
            "longmem_seed_batch_pairs": 2,
            "longmem_projection_key_sha256": "e" * 64,
            "provider_lanes": [
                {
                    "lane": "reader",
                    "provider": "trusted-provider",
                    "route_provider": "openai",
                    "receipt_provider": "OpenAI",
                    "profile_revision": "provider-v1",
                    "model": "provider/model",
                    "max_requests": 10,
                    "max_prompt_tokens": 10_000,
                    "max_completion_tokens": 2_000,
                    "max_total_tokens": 12_000,
                    "max_cost_usd_micros": 50_000,
                },
                {
                    "lane": "judge",
                    "provider": "trusted-provider",
                    "route_provider": "openai",
                    "receipt_provider": "OpenAI",
                    "profile_revision": "provider-v1",
                    "model": "provider/model",
                    "max_requests": 10,
                    "max_prompt_tokens": 10_000,
                    "max_completion_tokens": 2_000,
                    "max_total_tokens": 12_000,
                    "max_cost_usd_micros": 50_000,
                },
            ],
            "embedding_lane": {
                "lane": "embedding",
                "provider": "perplexity",
                "profile_revision": "embedding-profile-v1",
                "model": "perplexity/pplx-embed-v1-0.6b",
                "dimensions": 1024,
                "max_requests": 100,
                "max_input_tokens": 250_000,
                "max_cost_usd_micros": 50_000,
            },
            "ablation_profile_revision": "ablation-v1",
            "ablation_profile_checksum": "66" * 32,
            "ablation_dataset_sha256": "69" * 32,
            "ablation_threshold_manifest_sha256": "67" * 32,
            "ablation_selection_key_sha256": "68" * 32,
            "ablation_projection_key_sha256": "78" * 32,
            "ablation_coordinator_policy": {
                "sample_size": 2,
                "max_attempts": 2,
                "max_requests": 12,
                "request_timeout_milliseconds": 1_000,
                "total_timeout_milliseconds": 10_000,
            },
            "inference_ablation": {
                "intervention": "inference",
                "contract_version": "ablation-v1",
                "threshold_micros": 200_000,
                "budget": budget,
            },
            "embedding_ablation": {
                "intervention": "embedding",
                "contract_version": "ablation-v1",
                "threshold_micros": 100_000,
                "budget": budget,
            },
            "composite": {
                "schema_version": 1,
                "revision": "composite-v1",
                "formula_revision": "weighted-quality-gates-v1",
                "base_weight_bps": 7_500,
                "longmem_weight_bps": 2_500,
                "checksum": "88" * 32,
            },
        }
    )


def _readiness() -> V9ConfirmationScorerReadiness:
    return V9ConfirmationScorerReadiness(
        ready=True,
        profile_revision=_PROFILE_REVISION,
        profile_checksum=_PROFILE_CHECKSUM,
    )


def _job(
    slot_id: str,
    *,
    suffix: int = 1,
    deadline: datetime | None = None,
    profile: ConfirmationExecutionProfile | None = None,
) -> V9ConfirmationJobResponse:
    return V9ConfirmationJobResponse(
        purpose="v9_confirmation_bundle",
        bundle_id=UUID(f"10000000-0000-0000-0000-{suffix:012d}"),
        ticket_id=UUID(f"20000000-0000-0000-0000-{suffix:012d}"),
        reservation_id=UUID(f"30000000-0000-0000-0000-{suffix:012d}"),
        agent_id=UUID(f"40000000-0000-0000-0000-{suffix:012d}"),
        slot_id=slot_id,
        deadline=deadline or datetime.now(UTC) + timedelta(hours=2),
        artifact_sha256=_ARTIFACT_SHA,
        bench_version=9,
        settings_revision=7,
        settings_checksum="99" * 32,
        retest_generation=2,
        mode=ConfirmationBundleMode.SHADOW,
        per_bundle_request_cap=100,
        per_bundle_token_cap=250_000,
        execution_profile=profile or _profile(),
        inference_grants=[
            {
                "lane": lane,
                "grant_id": UUID(int=suffix * 10 + index),
                "bearer": f"grant-{lane}-" + ("x" * 32),
                "generation": 1,
                "proxy_url": "https://platform.test/api/v1/inference/confirmation/"
                + ("embeddings" if lane == "embedding" else "chat/completions"),
                "model": (
                    "perplexity/pplx-embed-v1-0.6b"
                    if lane == "embedding"
                    else "provider/model"
                ),
                "provider": "perplexity" if lane == "embedding" else "trusted-provider",
                "route_provider": "perplexity" if lane == "embedding" else "openai",
                "receipt_provider": "perplexity" if lane == "embedding" else "OpenAI",
                "profile_revision": (
                    "embedding-profile-v1" if lane == "embedding" else "provider-v1"
                ),
                "request_budget": 100,
                "token_budget": 250_000,
                "cost_budget_microusd": 50_000,
                "expires_at": deadline or datetime.now(UTC) + timedelta(hours=2),
            }
            for index, lane in enumerate(("reader", "judge", "embedding"), start=1)
        ],
    )


def _artifact(job: V9ConfirmationJobResponse) -> ArtifactResponse:
    image_sha = "aa" * 32
    return ArtifactResponse(
        agent_id=job.agent_id,
        sha256=job.artifact_sha256,
        download_url="https://storage.test/v9-agent.tar.gz?signature=opaque",
        expires_at=job.deadline,
        screened_image_url="https://storage.test/v9-image.tar?signature=opaque",
        screened_image_sha256=image_sha,
        screened_image_size_bytes=42_000,
        screened_image_id=f"sha256:{image_sha}",
        screened_image_ref="ditto-screen/agent:v9",
        bench_version=9,
        screening_policy_version=9,
    )


def _result() -> V9ConfirmationScorerResult:
    from ditto.tests.validator.test_v9_confirmation_transport import _scorer_result

    return _scorer_result()


def _prepared(job: V9ConfirmationJobResponse) -> V9ConfirmationPreparedReport:
    # Reuse the exact cross-boundary normalized root fixture so ordinary worker
    # lifecycle tests exercise the production typed prepare contract too.
    from ditto.tests.validator.test_v9_confirmation_transport import (
        _worker_prepared_report,
    )

    return _worker_prepared_report(job)[0]


def _worker(
    *, capacity: int = 2
) -> tuple[ValidatorWorker, MagicMock, MagicMock, _RecordingKeypair]:
    platform = MagicMock()
    platform.request_v9_confirmation_job = AsyncMock(return_value=None)
    platform.get_v9_confirmation_artifact = AsyncMock()
    platform.prepare_v9_confirmation_report = AsyncMock(
        side_effect=lambda job, _result: _prepared(job)
    )
    platform.submit_v9_confirmation_report = AsyncMock()
    platform.fail_v9_confirmation_job = AsyncMock()
    platform.request_v9_confirmation_job = AsyncMock(return_value=None)
    # These are sentinels: the private lane must never touch either score path.
    platform.submit_score = AsyncMock()
    platform.submit_top5_confirmation_score = AsyncMock()
    platform.report_ticket_failed = AsyncMock()
    dittobench = MagicMock()
    dittobench.v9_confirmation_readiness = AsyncMock(return_value=_readiness())
    dittobench.prepare_inference_session = AsyncMock(
        return_value=InferenceBrokerSession(
            session_id="confirmation-session-0001",
            activation_secret="s" * 32,
            broker_public_key=_BROKER_PUBLIC_KEY,
        )
    )
    dittobench.activate_confirmation_inference_session = AsyncMock()
    dittobench.cancel_inference_session = AsyncMock()
    dittobench.execute_v9_confirmation = AsyncMock(return_value=_result())
    keypair = _RecordingKeypair()
    worker = ValidatorWorker(
        config=_config(capacity=capacity),
        platform=platform,
        dittobench=dittobench,
        chain=MagicMock(),
        keypair=keypair,
    )
    return worker, platform, dittobench, keypair


def _assert_score_lanes_untouched(platform: MagicMock) -> None:
    platform.submit_score.assert_not_awaited()
    platform.submit_top5_confirmation_score.assert_not_awaited()
    platform.report_ticket_failed.assert_not_awaited()


class TestV9ConfirmationReadiness:
    async def test_unconfigured_scorer_claims_nothing(self) -> None:
        worker, platform, dittobench, _ = _worker()
        dittobench.v9_confirmation_readiness.return_value = None

        await worker._run_v9_confirmation_lane()

        platform.request_v9_confirmation_job.assert_not_awaited()
        _assert_score_lanes_untouched(platform)

    @pytest.mark.parametrize(
        "error",
        [
            ValidatorInfrastructureError("unreachable"),
            DittobenchError("invalid readiness"),
            TypeError("unawaitable test double"),
        ],
    )
    async def test_readiness_errors_fail_closed(self, error: Exception) -> None:
        worker, platform, dittobench, _ = _worker()
        dittobench.v9_confirmation_readiness.side_effect = error

        await worker._run_v9_confirmation_lane()

        platform.request_v9_confirmation_job.assert_not_awaited()
        _assert_score_lanes_untouched(platform)

    async def test_untyped_readiness_fails_closed(self) -> None:
        worker, platform, dittobench, _ = _worker()
        dittobench.v9_confirmation_readiness.return_value = SimpleNamespace(
            profile_revision=_PROFILE_REVISION,
            profile_checksum=_PROFILE_CHECKSUM,
        )

        await worker._run_v9_confirmation_lane()

        platform.request_v9_confirmation_job.assert_not_awaited()

    @pytest.mark.parametrize("gate", ["draining", "paused", "resource_constrained"])
    async def test_nonaccepting_admission_never_probes_or_claims(
        self, gate: str
    ) -> None:
        worker, platform, dittobench, _ = _worker()
        worker._admission = cast(Any, gate)

        await worker._run_v9_confirmation_lane()

        dittobench.v9_confirmation_readiness.assert_not_awaited()
        platform.request_v9_confirmation_job.assert_not_awaited()

    @pytest.mark.parametrize("event_name", ["stop", "drain"])
    async def test_stop_or_drain_forbids_new_claims(self, event_name: str) -> None:
        worker, platform, dittobench, _ = _worker()
        event = asyncio.Event()
        event.set()

        await worker._run_v9_confirmation_lane(
            stop_requested=event if event_name == "stop" else None,
            drain_requested=event if event_name == "drain" else None,
        )

        dittobench.v9_confirmation_readiness.assert_not_awaited()
        platform.request_v9_confirmation_job.assert_not_awaited()


class TestV9ConfirmationClaims:
    async def test_claims_once_per_healthy_idle_slot_with_exact_profile(self) -> None:
        worker, platform, _, _ = _worker(capacity=8)

        await worker._run_v9_confirmation_lane()

        assert platform.request_v9_confirmation_job.await_count == 4
        assert {
            tuple(sorted(call.kwargs.items()))
            for call in platform.request_v9_confirmation_job.await_args_list
        } == {
            tuple(
                sorted(
                    {
                        "slot_id": slot_id,
                        "profile_revision": _PROFILE_REVISION,
                        "profile_checksum": _PROFILE_CHECKSUM,
                        "broker_public_key": _BROKER_PUBLIC_KEY,
                    }.items()
                )
            )
            for slot_id in ("longmem-0", "longmem-1", "longmem-2", "longmem-3")
        }

    async def test_explicit_idle_slot_scope_does_not_fan_out(self) -> None:
        worker, platform, _, _ = _worker(capacity=8)

        await worker._run_v9_confirmation_lane(slot_ids=("longmem-2",))

        platform.request_v9_confirmation_job.assert_awaited_once_with(
            slot_id="longmem-2",
            profile_revision=_PROFILE_REVISION,
            profile_checksum=_PROFILE_CHECKSUM,
            broker_public_key=_BROKER_PUBLIC_KEY,
        )

    async def test_duplicate_slot_scope_never_multiplies_a_claim(self) -> None:
        worker, platform, _, _ = _worker(capacity=4)

        await worker._run_v9_confirmation_lane(
            slot_ids=("longmem-1", "longmem-1", "longmem-1")
        )

        platform.request_v9_confirmation_job.assert_awaited_once_with(
            slot_id="longmem-1",
            profile_revision=_PROFILE_REVISION,
            profile_checksum=_PROFILE_CHECKSUM,
            broker_public_key=_BROKER_PUBLIC_KEY,
        )

    async def test_canonical_occupancy_does_not_reduce_longmem_capacity(self) -> None:
        worker, platform, _, _ = _worker(capacity=4)
        worker._slots["slot-0"].active_agent_id = UUID(
            "40000000-0000-0000-0000-000000000099"
        )

        await worker._run_v9_confirmation_lane()

        assert platform.request_v9_confirmation_job.await_count == 2
        assert {
            call.kwargs["slot_id"]
            for call in platform.request_v9_confirmation_job.await_args_list
        } == {"longmem-0", "longmem-1"}

    async def test_ordinary_slot_scope_is_rejected_locally(self) -> None:
        worker, platform, _, _ = _worker(capacity=4)

        await worker._run_v9_confirmation_lane(slot_ids=("slot-0",))

        platform.request_v9_confirmation_job.assert_not_awaited()

    async def test_claim_204_is_an_idle_success(self) -> None:
        worker, platform, dittobench, _ = _worker(capacity=1)

        await worker._run_v9_confirmation_lane()

        platform.request_v9_confirmation_job.assert_awaited_once()
        platform.get_v9_confirmation_artifact.assert_not_awaited()
        dittobench.execute_v9_confirmation.assert_not_awaited()
        platform.submit_v9_confirmation_report.assert_not_awaited()
        _assert_score_lanes_untouched(platform)


class TestV9ConfirmationExecution:
    async def test_success_fetches_executes_signs_and_submits_exact_bundle(
        self,
    ) -> None:
        worker, platform, dittobench, keypair = _worker(capacity=1)
        job = _job("longmem-0")
        artifact = _artifact(job)
        platform.request_v9_confirmation_job.return_value = job
        platform.get_v9_confirmation_artifact.return_value = artifact

        await worker._run_v9_confirmation_lane()

        platform.get_v9_confirmation_artifact.assert_awaited_once_with(job)
        dittobench.execute_v9_confirmation.assert_awaited_once_with(
            job=job,
            artifact=artifact,
            inference_session_id="confirmation-session-0001",
        )
        platform.submit_v9_confirmation_report.assert_awaited_once()
        submitted_job, report = platform.submit_v9_confirmation_report.await_args.args
        assert submitted_job == job
        assert report.longmemeval == _prepared(job).longmemeval
        assert report.inference_ablation == _prepared(job).inference_ablation
        assert report.embedding_ablation == _prepared(job).embedding_ablation
        bundle_message = next(
            message
            for message in keypair.messages
            if message.startswith(b"validator-v9-confirmation:v1:")
        )
        assert report.bundle_signature == hashlib.sha512(bundle_message).hexdigest()
        signing_text = bundle_message.decode()
        assert signing_text.startswith("validator-v9-confirmation:v1:")
        assert str(job.bundle_id) in signing_text
        assert str(job.ticket_id) in signing_text
        assert _prepared(job).evidence_sha256 in signing_text
        _assert_score_lanes_untouched(platform)

    async def test_long_execution_keeps_validator_live_without_claiming_canonical_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker, platform, dittobench, _ = _worker(capacity=1)
        job = _job("longmem-0")
        platform.request_v9_confirmation_job.return_value = job
        platform.get_v9_confirmation_artifact.return_value = _artifact(job)
        release = asyncio.Event()

        async def execute(**_: object) -> V9ConfirmationScorerResult:
            await release.wait()
            return _result()

        dittobench.execute_v9_confirmation.side_effect = execute
        heartbeat = AsyncMock(return_value=True)
        worker._report_heartbeat = heartbeat  # type: ignore[method-assign]
        monkeypatch.setattr(worker_mod, "_ACTIVE_HEARTBEAT_SECONDS", 0.001)

        lane = asyncio.create_task(worker._run_v9_confirmation_lane())
        for _ in range(100):
            if heartbeat.await_count:
                break
            await asyncio.sleep(0.001)
        assert heartbeat.await_count >= 1
        assert all(call.args == ("polling",) for call in heartbeat.await_args_list)
        assert all(slot.active_agent_id is None for slot in worker._slots.values())
        release.set()
        await lane

        platform.submit_v9_confirmation_report.assert_awaited_once()

    @pytest.mark.parametrize(
        ("field", "bad_value"),
        [
            ("slot_id", "longmem-1"),
            ("bench_version", 8),
            ("purpose", "canonical_score"),
        ],
    )
    async def test_lease_identity_mismatch_never_fetches_or_executes(
        self, field: str, bad_value: object
    ) -> None:
        worker, platform, dittobench, _ = _worker(capacity=1)
        platform.request_v9_confirmation_job.return_value = _job(
            "longmem-0"
        ).model_copy(update={field: bad_value})

        await worker._run_v9_confirmation_lane()

        platform.get_v9_confirmation_artifact.assert_not_awaited()
        dittobench.execute_v9_confirmation.assert_not_awaited()
        platform.submit_v9_confirmation_report.assert_not_awaited()
        _assert_score_lanes_untouched(platform)

    @pytest.mark.parametrize(
        ("revision", "checksum"),
        [
            ("wrong-revision", _PROFILE_CHECKSUM),
            (_PROFILE_REVISION, "ff" * 32),
        ],
    )
    async def test_profile_mismatch_never_executes(
        self, revision: str, checksum: str
    ) -> None:
        worker, platform, dittobench, _ = _worker(capacity=1)
        platform.request_v9_confirmation_job.return_value = _job(
            "longmem-0", profile=_profile(revision=revision, checksum=checksum)
        )

        await worker._run_v9_confirmation_lane()

        platform.get_v9_confirmation_artifact.assert_not_awaited()
        dittobench.execute_v9_confirmation.assert_not_awaited()
        _assert_score_lanes_untouched(platform)

    async def test_expired_lease_never_fetches_or_executes(self) -> None:
        worker, platform, dittobench, _ = _worker(capacity=1)
        platform.request_v9_confirmation_job.return_value = _job(
            "longmem-0", deadline=datetime.now(UTC) - timedelta(seconds=1)
        )

        await worker._run_v9_confirmation_lane()

        platform.get_v9_confirmation_artifact.assert_not_awaited()
        dittobench.execute_v9_confirmation.assert_not_awaited()
        platform.submit_v9_confirmation_report.assert_not_awaited()
        _assert_score_lanes_untouched(platform)

    async def test_result_that_finishes_after_deadline_is_never_submitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker, platform, _, _ = _worker(capacity=1)
        job = _job("longmem-0")
        platform.request_v9_confirmation_job.return_value = job
        platform.get_v9_confirmation_artifact.return_value = _artifact(job)

        class _AfterDeadline:
            @staticmethod
            def now(tz: object = None) -> datetime:
                del tz
                return job.deadline + timedelta(microseconds=1)

        monkeypatch.setattr(worker_mod, "datetime", _AfterDeadline)

        await worker._run_v9_confirmation_lane()

        platform.submit_v9_confirmation_report.assert_not_awaited()
        _assert_score_lanes_untouched(platform)

    @pytest.mark.parametrize(
        "failing_stage", ["claim", "artifact", "execute", "submit"]
    )
    async def test_one_slot_error_does_not_cancel_a_successful_sibling(
        self, failing_stage: str
    ) -> None:
        worker, platform, dittobench, _ = _worker(capacity=4)
        jobs = {
            "longmem-0": _job("longmem-0", suffix=1),
            "longmem-1": _job("longmem-1", suffix=2),
        }

        async def claim(*, slot_id: str, **_: object) -> V9ConfirmationJobResponse:
            if failing_stage == "claim" and slot_id == "longmem-0":
                raise PlatformError("claim failed")
            return jobs[slot_id]

        async def artifact(job: V9ConfirmationJobResponse) -> ArtifactResponse:
            if failing_stage == "artifact" and job.slot_id == "longmem-0":
                raise PlatformError("artifact failed")
            return _artifact(job)

        async def execute(
            *,
            job: V9ConfirmationJobResponse,
            artifact: ArtifactResponse,
            inference_session_id: str,
        ) -> V9ConfirmationScorerResult:
            del artifact, inference_session_id
            if failing_stage == "execute" and job.slot_id == "longmem-0":
                raise DittobenchError("execute failed")
            return _result()

        async def submit(job: V9ConfirmationJobResponse, _report: object) -> None:
            if failing_stage == "submit" and job.slot_id == "longmem-0":
                raise PlatformError("submit failed")

        platform.request_v9_confirmation_job.side_effect = claim
        platform.get_v9_confirmation_artifact.side_effect = artifact
        dittobench.execute_v9_confirmation.side_effect = execute
        platform.submit_v9_confirmation_report.side_effect = submit

        await worker._run_v9_confirmation_lane()

        successful_submissions = [
            call.args[0].slot_id
            for call in platform.submit_v9_confirmation_report.await_args_list
            if call.args[0].slot_id == "longmem-1"
        ]
        assert successful_submissions == ["longmem-1"]
        _assert_score_lanes_untouched(platform)


class TestV9ConfirmationSweepIntegration:
    async def test_longmem_loop_runs_while_canonical_sweep_is_blocked(self) -> None:
        worker, _, _, _ = _worker(capacity=2)
        stop = asyncio.Event()
        canonical_started = asyncio.Event()
        release_canonical = asyncio.Event()
        longmem_started = asyncio.Event()

        async def blocked_canonical(**_: object) -> int:
            canonical_started.set()
            await release_canonical.wait()
            return 0

        async def observed_longmem(**_: object) -> None:
            longmem_started.set()
            await stop.wait()

        async def inert_weights(*_: object, **__: object) -> None:
            await stop.wait()

        worker.run_once = AsyncMock(side_effect=blocked_canonical)  # type: ignore[method-assign]
        worker._run_v9_confirmation_lane = AsyncMock(  # type: ignore[method-assign]
            side_effect=observed_longmem
        )
        worker._run_weights_forever = inert_weights  # type: ignore[method-assign]

        task = asyncio.create_task(worker.run_forever(stop))
        await asyncio.wait_for(canonical_started.wait(), timeout=1)
        await asyncio.wait_for(longmem_started.wait(), timeout=1)

        assert not release_canonical.is_set()
        worker._run_v9_confirmation_lane.assert_awaited_once()
        release_canonical.set()
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    async def test_drain_waits_for_active_longmem_execution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worker, _, _, _ = _worker(capacity=2)
        stop = asyncio.Event()
        drain = asyncio.Event()
        drain.set()
        states: list[str] = []
        worker._longmem_active = True
        worker._report_heartbeat = AsyncMock(return_value=True)  # type: ignore[method-assign]
        monkeypatch.setattr(
            worker_mod,
            "write_update_state",
            lambda state, **_kwargs: states.append(state),
        )

        task = asyncio.create_task(worker._acknowledge_drain(stop, drain))
        await asyncio.sleep(0.01)
        assert "drained" not in states

        worker._longmem_active = False
        for _ in range(100):
            if "drained" in states:
                break
            await asyncio.sleep(0.001)
        assert "drained" in states
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    async def test_canonical_sweep_does_not_inline_longmem_claims(self) -> None:
        worker, platform, dittobench, _ = _worker(capacity=1)
        order: list[str] = []

        async def request_job(*, slot_id: str) -> None:
            assert slot_id == "slot-0"
            order.append("canonical")
            return None

        async def readiness() -> None:
            order.append("readiness")
            return None

        platform.request_job = AsyncMock(side_effect=request_job)
        dittobench.v9_confirmation_readiness = AsyncMock(side_effect=readiness)
        worker._scoring_preflight = AsyncMock(return_value=True)  # type: ignore[method-assign]
        worker._report_heartbeat = AsyncMock(return_value=True)  # type: ignore[method-assign]
        worker._run_top5_confirmation_lane = AsyncMock()  # type: ignore[method-assign]

        await worker.run_once(set_weights=False)

        assert order == ["canonical"]
        dittobench.v9_confirmation_readiness.assert_not_awaited()
        worker._run_top5_confirmation_lane.assert_awaited_once()
        _assert_score_lanes_untouched(platform)

    async def test_v9_lane_does_not_reuse_or_multiply_top5_lane(self) -> None:
        worker, platform, _, _ = _worker(capacity=3)
        top5 = AsyncMock()
        worker._run_top5_confirmation_lane = top5  # type: ignore[method-assign]

        await worker._run_v9_confirmation_lane()

        assert platform.request_v9_confirmation_job.await_count == 2
        top5.assert_not_awaited()
        platform.submit_top5_confirmation_score.assert_not_awaited()
        platform.submit_score.assert_not_awaited()
