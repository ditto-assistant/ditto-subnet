"""The validator's generator-round lane: run one panel agent on a round dataset.

Platform routes a round panel run onto a free slot; the worker fetches the
artifact and the round dataset, runs them with dittobench-api's
``generator-round-v1`` mode, and posts per-case results to the round endpoint.
A failure is handed back on the round's own endpoint, never ``/job/fail``, so
the agent is never charged an attempt.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from ditto.api_models.generator_round_validator import (
    GeneratorRoundCaseResult,
    GeneratorRoundJobRequest,
    GeneratorRoundJobResponse,
    GeneratorRoundScoreRequest,
    GeneratorRoundScoreResponse,
    round_results_digest,
)
from ditto.api_models.validator import ArtifactResponse
from ditto.tests.validator.test_worker import _config, _platform_with_ledger
from ditto.validator.dittobench import DittobenchClient
from ditto.validator.errors import DittobenchError, ValidatorInfrastructureError
from ditto.validator.worker import ValidatorWorker

_DATASET = b'{"contract":"dittobench-generator-round/v1"}'


def _job(**overrides: object) -> GeneratorRoundJobResponse:
    base: dict[str, object] = {
        "round_index": 12,
        "agent_id": uuid4(),
        "slot_id": "slot-0",
        "miner_hotkey": "5PanelMiner0",
        "sha256": "ab" * 32,
        "deadline": datetime.now(UTC) + timedelta(minutes=90),
        "bench_version": 13,
        "dataset_sha256": hashlib.sha256(_DATASET).hexdigest(),
        "seed": 99,
        "case_count": 2,
    }
    base.update(overrides)
    return GeneratorRoundJobResponse.model_validate(base)


def _artifact(job: GeneratorRoundJobResponse) -> ArtifactResponse:
    return ArtifactResponse(
        agent_id=job.agent_id,
        sha256=job.sha256,
        download_url="https://signed.example/x.tar.gz",
        expires_at=datetime.now(UTC),
        # The artifact route answers with the lease's own bench version.
        bench_version=job.bench_version,
        screening_policy_version=9,
        screened_image_url="https://signed.example/image.tar",
        screened_image_sha256="12" * 32,
        screened_image_size_bytes=123,
        screened_image_id="sha256:" + "34" * 32,
        screened_image_ref="ditto-screen/x:latest",
    )


def _worker(job: GeneratorRoundJobResponse | None, dittobench: MagicMock):
    platform = _platform_with_ledger(jobs=[], ledger=[])
    platform.request_generator_round_job = AsyncMock(return_value=job)
    platform.get_artifact = AsyncMock(
        side_effect=lambda _agent_id: _artifact(job) if job else None
    )
    platform.get_private_dataset = AsyncMock(return_value=_DATASET)
    platform.submit_generator_round_score = AsyncMock(
        return_value=GeneratorRoundScoreResponse(
            round_index=12, agent_id=job.agent_id if job else uuid4(), accepted=True
        )
    )
    platform.fail_generator_round_job = AsyncMock(return_value=None)
    platform.report_ticket_failed = AsyncMock()
    config = _config()
    config.dittobench_mock = True  # no inference broker in unit tests
    worker = ValidatorWorker(
        config=config,
        platform=platform,
        dittobench=dittobench,
        chain=MagicMock(),
        keypair=MagicMock(sign=MagicMock(return_value=b"\x01" * 64)),
    )
    worker._scorer_runs_generator_rounds = True
    return worker, platform


async def test_round_run_posts_results_and_frees_the_slot() -> None:
    job = _job()
    dittobench = MagicMock()
    dittobench.run_generator_round = AsyncMock(
        return_value=(
            "run-1",
            [
                {"case_id": "c0/a", "score": 1.0, "category": "generator_round"},
                {"case_id": "c0/b", "score": 0, "category": "generator_round_fault"},
            ],
        )
    )
    worker, platform = _worker(job, dittobench)

    assert await worker._run_generator_round_lane() is True

    platform.request_generator_round_job.assert_awaited_once_with(slot_id="slot-0")
    platform.get_private_dataset.assert_awaited_once_with(
        job.agent_id, dataset_sha256=job.dataset_sha256, deadline=job.deadline
    )
    kwargs = dittobench.run_generator_round.await_args.kwargs
    assert kwargs["dataset_bytes"] == _DATASET and kwargs["seed"] == 99
    assert kwargs["screened_image_url"] == "https://signed.example/image.tar"
    submitted = platform.submit_generator_round_score.await_args
    assert submitted.kwargs["run_id"] == "run-1"
    assert submitted.kwargs["results"] == [
        GeneratorRoundCaseResult(case_id="c0/a", score=1.0, fault=False),
        GeneratorRoundCaseResult(case_id="c0/b", score=0.0, fault=True),
    ]
    platform.fail_generator_round_job.assert_not_awaited()
    platform.report_ticket_failed.assert_not_awaited()
    assert worker._slots["slot-0"].active_agent_id is None


async def test_no_round_work_is_quiet() -> None:
    worker, platform = _worker(None, MagicMock())
    assert await worker._run_generator_round_lane() is False
    platform.fail_generator_round_job.assert_not_awaited()


async def test_a_scorer_without_round_mode_never_asks() -> None:
    worker, platform = _worker(_job(), MagicMock())
    worker._scorer_runs_generator_rounds = False
    assert await worker._run_generator_round_lane() is False
    platform.request_generator_round_job.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "reason", "slot_kept"),
    [
        (DittobenchError("harness never became healthy"), "scoring_error", True),
        (ValidatorInfrastructureError("scorer down"), "infrastructure", False),
    ],
)
async def test_failures_go_to_the_round_endpoint_never_job_fail(
    error: Exception, reason: str, slot_kept: bool
) -> None:
    job = _job()
    dittobench = MagicMock()
    dittobench.run_generator_round = AsyncMock(side_effect=error)
    worker, platform = _worker(job, dittobench)

    assert await worker._run_generator_round_lane() is True

    platform.fail_generator_round_job.assert_awaited_once()
    assert platform.fail_generator_round_job.await_args.kwargs["reason"] == reason
    platform.report_ticket_failed.assert_not_awaited()
    platform.submit_generator_round_score.assert_not_awaited()
    assert ("slot-0" in worker._healthy_slots) is slot_kept


async def test_a_lease_for_a_slot_it_did_not_offer_is_refused() -> None:
    job = _job(slot_id="slot-1")
    dittobench = MagicMock()
    dittobench.run_generator_round = AsyncMock()
    worker, platform = _worker(job, dittobench)

    assert await worker._run_generator_round_lane() is True

    platform.request_generator_round_job.assert_awaited_once_with(slot_id="slot-0")
    dittobench.run_generator_round.assert_not_awaited()
    assert platform.fail_generator_round_job.await_args.kwargs["detail"] == (
        "round_job_not_executable"
    )


async def test_artifact_identity_mismatch_is_refused() -> None:
    job = _job()
    dittobench = MagicMock()
    dittobench.run_generator_round = AsyncMock()
    worker, platform = _worker(job, dittobench)
    platform.get_artifact = AsyncMock(
        return_value=_artifact(job).model_copy(update={"sha256": "cd" * 32})
    )

    await worker._run_generator_round_lane()

    dittobench.run_generator_round.assert_not_awaited()
    assert (
        platform.fail_generator_round_job.await_args.kwargs["reason"] == "scoring_error"
    )


async def test_a_round_runs_at_the_bench_version_platform_froze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job carries the round's frozen contract; the worker runs at it
    instead of a hard-wired v13."""
    import ditto.validator.worker as worker_mod

    monkeypatch.setattr(worker_mod, "GENERATOR_ROUND_BENCH_VERSIONS", (13, 14))
    monkeypatch.setattr(worker_mod, "SUPPORTED_BENCH_VERSIONS", frozenset({13, 14}))
    job = _job(bench_version=14)
    dittobench = MagicMock()
    dittobench.run_generator_round = AsyncMock(return_value=("run-1", []))
    worker, platform = _worker(job, dittobench)

    assert await worker._run_generator_round_lane() is True

    assert dittobench.run_generator_round.await_args.kwargs["bench_version"] == 14
    platform.fail_generator_round_job.assert_not_awaited()


@pytest.mark.parametrize(
    "bench_version",
    [
        12,  # older than every round contract
        14,  # no round contract today (packs grade at v13)
    ],
)
async def test_a_round_job_outside_the_round_contracts_is_refused(
    bench_version: int,
) -> None:
    job = _job(bench_version=bench_version)
    dittobench = MagicMock()
    dittobench.run_generator_round = AsyncMock()
    worker, platform = _worker(job, dittobench)

    assert await worker._run_generator_round_lane() is True

    dittobench.run_generator_round.assert_not_awaited()
    assert platform.fail_generator_round_job.await_args.kwargs["detail"] == (
        "round_job_not_executable"
    )


async def test_an_artifact_at_another_bench_version_is_refused() -> None:
    job = _job()
    dittobench = MagicMock()
    dittobench.run_generator_round = AsyncMock()
    worker, platform = _worker(job, dittobench)
    platform.get_artifact = AsyncMock(
        return_value=_artifact(job).model_copy(update={"bench_version": 12})
    )

    await worker._run_generator_round_lane()

    dittobench.run_generator_round.assert_not_awaited()
    assert (
        platform.fail_generator_round_job.await_args.kwargs["reason"] == "scoring_error"
    )


def test_signing_messages_bind_every_field() -> None:
    agent = uuid4()
    request = GeneratorRoundJobRequest(
        validator_hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        slot_id="slot-1",
        nonce=uuid4(),
        requested_at=datetime(2026, 10, 24, tzinfo=UTC),
    )
    assert request.signing_message().startswith(
        b"validator-generator-round-job:v1:5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY:slot-1:"
    )
    results = [GeneratorRoundCaseResult(case_id="c0/a", score=0.5)]
    score = GeneratorRoundScoreRequest(
        validator_hotkey=request.validator_hotkey,
        round_index=3,
        ticket_deadline=datetime(2026, 10, 24, tzinfo=UTC),
        dataset_sha256="ef" * 32,
        run_id="r",
        results=results,
    )
    message = score.signing_message(agent)
    assert str(agent).encode() in message
    assert round_results_digest(results).encode() in message
    tampered = score.model_copy(
        update={"results": [GeneratorRoundCaseResult(case_id="c0/a", score=0.6)]}
    )
    assert tampered.signing_message(agent) != message


async def test_dittobench_round_submission_uses_the_round_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DittobenchClient.__new__(DittobenchClient)
    client._config = MagicMock(dittobench_mock=False)  # type: ignore[attr-defined]
    submitted: dict[str, object] = {}

    async def _submit(**kwargs: object) -> str:
        submitted.update(kwargs)
        return "run-9"

    async def _poll_run(*_args: object, **kwargs: object) -> dict[str, object]:
        assert kwargs["raw"] is True and kwargs["expected_bench_version"] == 13
        return {"run_id": "run-9", "per_case": [{"case_id": "c0/a", "score": 1.0}]}

    monkeypatch.setattr(client, "_submit", _submit)
    monkeypatch.setattr(client, "_poll_run", _poll_run)
    deadline = datetime.now(UTC) + timedelta(minutes=30)
    run_id, per_case = await client.run_generator_round(
        tarball_url="https://x",
        tarball_sha256="ab" * 32,
        dataset_bytes=_DATASET,
        dataset_sha256=hashlib.sha256(_DATASET).hexdigest(),
        seed=5,
        screened_image_url="https://i",
        screened_image_sha256="12" * 32,
        screened_image_size_bytes=1,
        screened_image_id="sha256:" + "34" * 32,
        screened_image_ref="r",
        inference_session_id=None,
        inference_grant_id=None,
        inference_agent_id=None,
        inference_slot_id=None,
        inference_ticket_deadline=None,
        ticket_deadline=deadline,
    )
    assert (run_id, per_case) == ("run-9", [{"case_id": "c0/a", "score": 1.0}])
    assert submitted["private_dataset_mode"] == "generator-round-v1"
    assert submitted["bench_version"] == 13
