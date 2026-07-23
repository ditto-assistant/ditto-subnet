"""Tests for the validator's dittobench-api request contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import httpx
import pytest

from ditto.api_models.validator_capabilities import (
    ValidatorComponentIdentity,
    ValidatorStackComponents,
    ValidatorStackIdentity,
)
from ditto.validator.dittobench import (
    DittobenchClient,
    DittobenchProgressSnapshot,
    InferenceBrokerSession,
    safe_progress_snapshot,
)
from ditto.validator.errors import DittobenchError, ValidatorInfrastructureError

_REVISION = "ab" * 20


@pytest.mark.asyncio
async def test_activate_inference_session_binds_dynamic_route_to_trusted_broker() -> (
    None
):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"active": True})

    config = SimpleNamespace(dittobench_api_url="http://dittobench.test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        await client.activate_inference_session(
            InferenceBrokerSession(
                session_id="session",
                activation_secret="activation",
                broker_public_key="public",
            ),
            grant_id=UUID("00000000-0000-0000-0000-000000000001"),
            agent_id=UUID("00000000-0000-0000-0000-000000000002"),
            slot_id="slot-3",
            ticket_deadline=datetime(2026, 7, 21, 22, 0, tzinfo=UTC),
            bearer="platform-bearer-never-forwarded",
            proxy_url="https://platform.example/api/v1/inference/chat/completions",
            generation=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=1),
            provider="WandB",
            profile_revision="openrouter-route-wandb-v1",
            model="openai/gpt-oss-20b",
        )

    assert captured["provider"] == "WandB"
    assert captured["profile_revision"] == "openrouter-route-wandb-v1"
    assert captured["model"] == "openai/gpt-oss-20b"
    assert captured["agent_id"] == "00000000-0000-0000-0000-000000000002"
    assert captured["slot_id"] == "slot-3"
    assert captured["ticket_deadline"] == "2026-07-21T22:00:00+00:00"


def _stack(revision: str = _REVISION) -> ValidatorStackIdentity:
    component = lambda rev=None: ValidatorComponentIdentity(  # noqa: E731
        source_revision=rev or _REVISION,
        version="source-build",
        provenance="committed_pin",
    )
    return ValidatorStackIdentity(
        mode="source",
        compose_schema=1,
        components=ValidatorStackComponents(
            ditto_subnet=component(),
            dittobench_api=component(revision),
            sandbox_docker=component(),
            model_relay=component(),
            pylon=component(),
            ollama=component(),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "status_code",
        "source_revision",
        "advertised",
        "expected_status",
        "expected_versions",
    ),
    [
        (200, _REVISION, [2, 3], "fresh_verified", (2, 3)),
        # A scorer that has rolled forward to v4 must stay fresh_verified with
        # every advertised version intact; degrading to unreachable/v2 here
        # would stall the in-flight rollout for the whole subnet.
        (200, _REVISION, [2, 3, 4], "fresh_verified", (2, 3, 4)),
        (200, _REVISION, [4], "fresh_verified", (4,)),
        # Keep the deployed v6 scorer capability fresh.
        (200, _REVISION, [2, 3, 4, 5, 6], "fresh_verified", (2, 3, 4, 5, 6)),
        (200, _REVISION, [5], "fresh_verified", (5,)),
        (200, _REVISION, [6], "fresh_verified", (6,)),
        # v7 stays dark unless the scorer also supplies its reviewed manifest
        # digest and exact provider/profile/model route identities.
        (200, _REVISION, [2, 3, 4, 5, 6, 7], "fresh_verified", (2, 3, 4, 5, 6)),
        # Project away unknown future contracts while preserving the versions
        # this validator and scorer can safely negotiate.
        (200, _REVISION, [2, 3, 4, 5, 6, 7, 8], "fresh_verified", (2, 3, 4, 5, 6)),
        # Unknown historical/gap versions remain malformed.
        (200, _REVISION, [1, 2, 3, 4, 5, 6], "unreachable", (2,)),
        # A future-only scorer has no mutually supported contract.
        (200, _REVISION, [8], "unreachable", (2,)),
        (200, "cd" * 20, [2, 3, 4], "identity_mismatch", (2,)),
        (404, _REVISION, [2, 3], "legacy_v2", (2,)),
        (503, _REVISION, [2, 3], "unreachable", (2,)),
    ],
)
async def test_secretless_scorer_capability_is_provenance_bound(
    status_code: int,
    source_revision: str,
    advertised: list[int],
    expected_status: str,
    expected_versions: tuple[int, ...],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(
            status_code,
            json={
                "software_version": "1.2.3",
                "source_revision": source_revision,
                "supported_bench_versions": advertised,
            },
        )

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_mock=False,
        dittobench_capabilities_timeout_seconds=1,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        observed = await DittobenchClient(config, http).scorer_benchmark_capability(  # type: ignore[arg-type]
            _stack()
        )
    assert observed.status == expected_status
    assert observed.supported_bench_versions == expected_versions


@pytest.mark.asyncio
async def test_future_scorer_version_preserves_negotiated_run_capacity() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "software_version": "0.22.0",
                "source_revision": _REVISION,
                "supported_bench_versions": [2, 3, 4, 5, 6, 7, 8],
                "full_run_capacity": 2,
            },
        )

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_mock=False,
        dittobench_capabilities_timeout_seconds=1,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        observed = await client.scorer_benchmark_capability(_stack())

    assert observed.status == "fresh_verified"
    assert observed.supported_bench_versions == (2, 3, 4, 5, 6)
    assert client.full_run_capacity == 2


@pytest.mark.asyncio
async def test_v7_capability_propagates_exact_calibration_identity() -> None:
    routes = [
        {
            "provider": "wandb",
            "profile_revision": "openrouter-wandb-gpt-oss-20b-v1",
            "model": "openai/gpt-oss-20b",
        },
        {
            "provider": "amazon-bedrock",
            "profile_revision": "openrouter-amazon-bedrock-gpt-oss-20b-v1",
            "model": "openai/gpt-oss-20b",
        },
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "software_version": "1.2.3",
                "source_revision": _REVISION,
                "supported_bench_versions": [2, 3, 4, 5, 6, 7],
                "full_run_capacity": 2,
                "v7_calibration": {
                    "manifest_sha256": "12" * 32,
                    "supported_routes": routes,
                },
            },
        )

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_mock=False,
        dittobench_capabilities_timeout_seconds=1,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        observed = await client.scorer_benchmark_capability(_stack())

    assert observed.supported_bench_versions == (2, 3, 4, 5, 6, 7)
    assert observed.v7_calibration is not None
    assert observed.v7_calibration.manifest_sha256 == "12" * 32
    assert [route.provider for route in observed.v7_calibration.supported_routes] == [
        "amazon-bedrock",
        "wandb",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("bench_version", [3, 4, 5, 6, 7])
async def test_v3_plus_uses_versioned_route_and_binds_request(
    bench_version: int,
) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"run_id": "run-v3"})

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test", run_size="full"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await DittobenchClient(config, http)._submit(  # type: ignore[arg-type]
            tarball_url="https://example.test/agent.tgz",
            dataset_sha256="12" * 32,
            bench_version=bench_version,
            screened_image_url="https://example.test/image.tar",
            screened_image_sha256="34" * 32,
            screened_image_size_bytes=123,
            screened_image_id="sha256:" + "56" * 32,
            screened_image_ref=(
                "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
            ),
        )
    assert seen["path"] == "/v2/score"
    assert cast(dict[str, object], seen["body"])["bench_version"] == bench_version


@pytest.mark.asyncio
@pytest.mark.parametrize("bench_version", [None, 0, 1, 8])
async def test_submit_rejects_missing_or_unsupported_benchmark_version(
    bench_version: int | None,
) -> None:
    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test", run_size="full"
    )
    async with httpx.AsyncClient() as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        with pytest.raises(DittobenchError, match="unsupported benchmark version"):
            await client._submit(
                tarball_url="https://example.test/agent.tgz",
                bench_version=bench_version,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
async def test_submit_admission_failure_is_validator_infrastructure(
    status: int,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="unavailable")

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test", run_size="full"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(
            ValidatorInfrastructureError,
            match=rf"scorer admission unavailable \({status}\)",
        ):
            await DittobenchClient(config, http)._submit(  # type: ignore[arg-type]
                tarball_url="https://example.test/agent.tgz", bench_version=2
            )


@pytest.mark.parametrize(
    ("status", "stage", "expected_counts"),
    [
        ("queued", "preparing", (None, None)),
        ("building", "building_harness", (None, None)),
        ("generating", "generating_dataset", (None, None)),
        ("seeding", "starting_harness", (None, None)),
        ("running", "running_benchmark", (51, 114)),
        ("scoring", "finalizing", (114, 114)),
        ("done", "finalizing", (114, 114)),
        ("failed", "failed_retrying", (None, None)),
    ],
)
def test_safe_progress_status_mapping(
    status: str, stage: str, expected_counts: tuple[int | None, int | None]
) -> None:
    done = 114 if status in {"scoring", "done"} else 51
    snapshot = safe_progress_snapshot(
        {
            "status": status,
            "progress": {"stage": "private", "done": done, "total": 114},
            "partial": [{"case_id": "private-case"}],
            "seed": 8675309,
            "run_id": "private-run",
            "error": "private error body",
        }
    )
    assert snapshot is not None
    assert snapshot.stage == stage
    assert (snapshot.completed, snapshot.total) == expected_counts


@pytest.mark.parametrize(
    ("completed", "total"),
    [
        (float("nan"), 114),
        (1, float("inf")),
        (True, 114),
        (-1, 114),
        (115, 114),
        (1, 10_001),
        ("51", 114),
    ],
)
def test_malformed_source_counts_degrade_to_unknown(
    completed: object, total: object
) -> None:
    snapshot = safe_progress_snapshot(
        {"status": "running", "progress": {"done": completed, "total": total}}
    )
    assert snapshot is not None
    assert snapshot.stage == "running_benchmark"
    assert snapshot.completed is None
    assert snapshot.total is None


@pytest.mark.asyncio
async def test_submit_does_not_forward_model_provider_credentials() -> None:
    request_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_body.update(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "run-1"})

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        run_size="full",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        run_id = await client._submit(
            tarball_url="https://example.test/agent.tgz", bench_version=2
        )

    assert run_id == "run-1"
    assert request_body == {
        "tarball_url": "https://example.test/agent.tgz",
        "run_size": "full",
    }


@pytest.mark.asyncio
async def test_v7_submit_echoes_exact_ticket_inference_identity() -> None:
    request_body: dict[str, object] = {}
    request_path = ""
    grant_id = UUID("00000000-0000-0000-0000-000000000001")
    agent_id = UUID("00000000-0000-0000-0000-000000000002")
    deadline = datetime(2026, 7, 21, 22, 0, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_path
        request_path = request.url.path
        request_body.update(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "run-v7"})

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        run_size="full",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        await client._submit(
            tarball_url="https://example.test/agent.tgz",
            bench_version=7,
            seed=1,
            dataset_sha256="12" * 32,
            screened_image_url="https://example.test/image.tar",
            screened_image_sha256="34" * 32,
            screened_image_size_bytes=123,
            screened_image_id="sha256:" + "56" * 32,
            screened_image_ref=(
                "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
            ),
            inference_session_id="session-v7",
            inference_grant_id=grant_id,
            inference_agent_id=agent_id,
            inference_slot_id="slot-3",
            inference_ticket_deadline=deadline,
        )

    assert request_path == "/v2/score"
    assert request_body["inference_session_id"] == "session-v7"
    assert request_body["inference_grant_id"] == str(grant_id)
    assert request_body["inference_agent_id"] == str(agent_id)
    assert request_body["inference_slot_id"] == "slot-3"
    assert request_body["inference_ticket_deadline"] == deadline.isoformat()


@pytest.mark.asyncio
async def test_v7_submit_rejects_incomplete_ticket_inference_identity() -> None:
    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        run_size="full",
    )
    async with httpx.AsyncClient() as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        with pytest.raises(DittobenchError, match="identity must be complete"):
            await client._submit(
                tarball_url="https://example.test/agent.tgz",
                bench_version=7,
                dataset_sha256="12" * 32,
                screened_image_url="https://example.test/image.tar",
                screened_image_sha256="34" * 32,
                screened_image_size_bytes=123,
                screened_image_id="sha256:" + "56" * 32,
                screened_image_ref=(
                    "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
                ),
                inference_session_id="session-v7",
                inference_grant_id=UUID("00000000-0000-0000-0000-000000000001"),
            )


@pytest.mark.asyncio
async def test_submit_forwards_verified_screened_image_contract() -> None:
    request_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_body.update(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "run-image"})

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        run_size="full",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        await client._submit(
            tarball_url="https://example.test/agent.tgz",
            bench_version=2,
            tarball_sha256="ab" * 32,
            screened_image_url="https://example.test/image.tar",
            screened_image_sha256="12" * 32,
            screened_image_size_bytes=123,
            screened_image_id="sha256:" + "34" * 32,
            screened_image_ref=(
                "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
            ),
        )

    assert request_body["screened_image_url"] == "https://example.test/image.tar"
    assert request_body["screened_image_sha256"] == "12" * 32
    assert request_body["screened_image_size_bytes"] == 123
    assert request_body["screened_image_id"] == "sha256:" + "34" * 32
    assert request_body["screened_image_ref"] == (
        "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
    )


@pytest.mark.asyncio
async def test_submit_rejects_partial_screened_image_contract_before_request() -> None:
    requested = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(202, json={"run_id": "should-not-run"})

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        run_size="full",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        with pytest.raises(DittobenchError, match="must be complete"):
            await client._submit(
                tarball_url="https://example.test/agent.tgz",
                bench_version=2,
                screened_image_url="https://example.test/image.tar",
            )

    assert requested is False


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_field", ["url", "sha256", "id", "ref"])
async def test_submit_rejects_empty_screened_image_identity(empty_field: str) -> None:
    requested = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(202, json={"run_id": "should-not-run"})

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        run_size="full",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        with pytest.raises(DittobenchError, match="cannot be empty"):
            await client._submit(
                tarball_url="https://example.test/agent.tgz",
                bench_version=2,
                screened_image_url=(
                    "" if empty_field == "url" else "https://example.test/image.tar"
                ),
                screened_image_sha256="" if empty_field == "sha256" else "12" * 32,
                screened_image_size_bytes=123,
                screened_image_id=(
                    "" if empty_field == "id" else "sha256:" + "34" * 32
                ),
                screened_image_ref=(
                    ""
                    if empty_field == "ref"
                    else "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
                ),
            )

    assert requested is False


@pytest.mark.asyncio
async def test_timeout_cancels_background_run() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "DELETE":
            return httpx.Response(202, json={"status": "failed"})
        return httpx.Response(200, json={"run_id": "run-1", "status": "running"})

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_timeout_seconds=0.01,
        dittobench_poll_seconds=0.02,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        with pytest.raises(DittobenchError, match="did not finish"):
            await client._poll("run-1", expected_bench_version=2)

    assert methods == ["GET", "DELETE"]


@pytest.mark.asyncio
async def test_timeout_tolerates_older_scorer_without_cancel_route() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(405)
        return httpx.Response(200, json={"run_id": "run-1", "status": "running"})

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_timeout_seconds=0.01,
        dittobench_poll_seconds=0.02,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        with pytest.raises(DittobenchError, match="did not finish"):
            await client._poll("run-1", expected_bench_version=2)


def _done_job() -> dict[str, object]:
    return {
        "status": "done",
        "bench_version": 2,
        "run_id": "private-run-id",
        "seed": 42,
        "error": "private error body",
        "partial": [{"case_id": "private-case", "expected": ["private-tool"]}],
        "progress": {"stage": "scoring", "done": 114, "total": 114},
        "report": {
            "run_id": "private-run-id",
            "composite": 0.9,
            "tool_mean": 0.9,
            "memory_mean": 0.9,
            "median_ms": 100,
            "n": 114,
            "generated_at": "2026-07-14T12:00:00Z",
            "per_case": [],
            "details": {"bench_version": 2},
        },
    }


def _poll_config() -> SimpleNamespace:
    return SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_timeout_seconds=1.0,
        dittobench_poll_seconds=0.0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected", "job_version", "report_version"),
    [(3, 2, 3), (3, 3, 2), (4, 3, 4), (4, 4, 3), (4, 2, 2)],
)
async def test_v3_plus_poll_rejects_job_or_report_version_mismatch(
    expected: int, job_version: int, report_version: int
) -> None:
    payload = _done_job()
    payload["bench_version"] = job_version
    report = cast(dict[str, object], payload["report"])
    report["details"] = {"bench_version": report_version}

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(cast(Any, _poll_config()), http)
        with pytest.raises(DittobenchError, match="benchmark version mismatch"):
            await client._poll("run-v3", expected_bench_version=expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("bench_version", [3, 4, 5, 6])
async def test_v3_plus_poll_returns_version_bound_report(bench_version: int) -> None:
    payload = _done_job()
    payload["bench_version"] = bench_version
    report = cast(dict[str, object], payload["report"])
    report["details"] = {"bench_version": bench_version}

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await DittobenchClient(cast(Any, _poll_config()), http)._poll(
            "run-v3", expected_bench_version=bench_version
        )
    assert result.bench_version == bench_version


@pytest.mark.asyncio
@pytest.mark.parametrize("expected_bench_version", [None, 0, 1, 8])
async def test_poll_rejects_missing_or_unsupported_expected_version(
    expected_bench_version: int | None,
) -> None:
    async with httpx.AsyncClient() as http:
        client = DittobenchClient(cast(Any, _poll_config()), http)
        with pytest.raises(DittobenchError, match="unsupported benchmark version"):
            await client._poll(
                "run-unknown", expected_bench_version=expected_bench_version
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job_version", "report_version"),
    [(None, 2), (2, None), (None, None), (3, 2), (2, 3)],
)
async def test_v2_poll_requires_explicit_matching_versions(
    job_version: int | None, report_version: int | None
) -> None:
    payload = _done_job()
    payload["bench_version"] = job_version
    report = cast(dict[str, object], payload["report"])
    report["details"] = {"bench_version": report_version}

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(cast(Any, _poll_config()), http)
        with pytest.raises(DittobenchError, match="benchmark version mismatch"):
            await client._poll("run-v2", expected_bench_version=2)


def _preflight_config() -> SimpleNamespace:
    return SimpleNamespace(
        dittobench_mock=False,
        dittobench_api_url="http://sandbox-docker:8000",
        embed_preflight_url="http://sandbox-docker:11434/api/embed",
        embed_preflight_timeout_seconds=5.0,
    )


@pytest.mark.asyncio
async def test_embedding_preflight_accepts_nonempty_vector() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/relay-preflight":
            return httpx.Response(200, json={"status": "ok"})
        seen.append(str(request.url))
        assert json.loads(request.content) == {
            "model": "embeddinggemma",
            "input": "validator preflight",
        }
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await DittobenchClient(cast(Any, _preflight_config()), http).preflight()

    assert seen == ["http://sandbox-docker:11434/api/embed"]


@pytest.mark.asyncio
async def test_embedding_preflight_unavailable_is_retryable_infrastructure() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="forwarder unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValidatorInfrastructureError, match=r"rejected \(503\)"):
            await DittobenchClient(cast(Any, _preflight_config()), http).preflight()


@pytest.mark.asyncio
async def test_embedding_preflight_timeout_is_retryable_infrastructure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow embedding route", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValidatorInfrastructureError, match="timed out"):
            await DittobenchClient(cast(Any, _preflight_config()), http).preflight()


@pytest.mark.asyncio
async def test_embedding_preflight_recovers_on_next_sweep_probe() -> None:
    responses = [
        httpx.Response(503, text="forwarder unavailable"),
        httpx.Response(200, json={"embeddings": [[0.1]]}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/relay-preflight":
            return httpx.Response(200, json={"status": "ok"})
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(cast(Any, _preflight_config()), http)
        with pytest.raises(ValidatorInfrastructureError):
            await client.preflight()
        await client.preflight()


@pytest.mark.asyncio
async def test_preclaim_check_does_not_touch_deprecated_relay() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v1/relay-preflight":
            return httpx.Response(
                503,
                json={
                    "status": "unavailable",
                    "failure": {
                        "kind": "validator_infrastructure",
                        "code": "model_relay_unavailable",
                        "retryable": True,
                    },
                },
            )
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await DittobenchClient(cast(Any, _preflight_config()), http).preflight()
    assert "/v1/relay-preflight" not in seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relay_response",
    [
        # Older scorer without the endpoint: fall back to the per-run signal.
        httpx.Response(404, text="not found"),
        # A 503 that is not the infrastructure envelope must not block claims.
        httpx.Response(503, json={"status": "unavailable"}),
        httpx.Response(503, text="gateway error"),
        # Healthy relay.
        httpx.Response(200, json={"status": "ok", "provider": "openrouter"}),
    ],
)
async def test_relay_preflight_is_additive_and_does_not_over_block(
    relay_response: httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/relay-preflight":
            return relay_response
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        # No raise: none of these are a validator_infrastructure envelope.
        await DittobenchClient(cast(Any, _preflight_config()), http).preflight()


@pytest.mark.asyncio
async def test_relay_preflight_transport_blip_does_not_block_claim() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/relay-preflight":
            raise httpx.ConnectError("transient", request=request)
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        # A blip reaching the scorer here is left to the per-run signal.
        await DittobenchClient(cast(Any, _preflight_config()), http).preflight()


@pytest.mark.asyncio
async def test_ollama_run_failure_is_retryable_infrastructure() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "failed",
                "error": (
                    "seeding secondary isolation graph failed: /seed returned "
                    "500: embedding error: ollama embed request to "
                    "http://host.docker.internal:11434/api/embed failed"
                ),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValidatorInfrastructureError, match="embedding"):
            await DittobenchClient(cast(Any, _poll_config()), http)._poll(
                "run-1", expected_bench_version=2
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "sandbox_oom",
        "sandbox_tmpfs_exhausted",
        "sandbox_network_unavailable",
        "model_relay_unavailable",
    ],
)
async def test_sandbox_resource_failure_is_retryable_infrastructure(
    code: str,
) -> None:
    private_marker = "private-container-id-and-miner-output"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "failed",
                "error": private_marker,
                "failure": {
                    "kind": "validator_infrastructure",
                    "code": code,
                    "retryable": True,
                    "diagnostics": {
                        "oom_killed": code == "sandbox_oom",
                        "memory_peak_bytes": 3 << 30,
                    },
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValidatorInfrastructureError, match=code) as caught:
            await DittobenchClient(cast(Any, _poll_config()), http)._poll(
                "run-1", expected_bench_version=3
            )
    assert private_marker not in str(caught.value)


@pytest.mark.asyncio
async def test_unknown_sandbox_failure_code_is_not_retryable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "failed",
                "error": "miner runtime exited",
                "failure": {
                    "kind": "validator_infrastructure",
                    "code": "arbitrary_code",
                    "retryable": True,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DittobenchError, match="miner runtime exited"):
            await DittobenchClient(cast(Any, _poll_config()), http)._poll(
                "run-1", expected_bench_version=3
            )


@pytest.mark.asyncio
async def test_poll_callback_receives_only_allowlisted_snapshot() -> None:
    seen: list[dict[str, object]] = []

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_done_job())

    async def progress(snapshot: DittobenchProgressSnapshot) -> None:
        seen.append(asdict(snapshot))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        report = await DittobenchClient(cast(Any, _poll_config()), http)._poll(
            "private-run-id", progress_callback=progress, expected_bench_version=2
        )

    assert report.n == 114
    # run_token is the opaque sha256 prefix of the run id, stable for the run and
    # never the raw id itself (the id stays private).
    expected_token = hashlib.sha256(b"private-run-id").hexdigest()[:16]
    assert seen == [
        {
            "stage": "finalizing",
            "completed": 114,
            "total": 114,
            "run_token": expected_token,
        }
    ]
    serialized = json.dumps(seen)
    assert "private-run-id" not in serialized
    for forbidden in ("case_id", "expected", "seed", "run_id", "error", "partial"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_progress_callback_failure_cannot_abort_completed_report() -> None:
    responses = [
        {"status": "queued"},
        {"status": "building"},
        {"status": "generating"},
        {"status": "running", "progress": {"done": 51, "total": 114}},
        {"status": "scoring", "progress": {"done": 114, "total": 114}},
        _done_job(),
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses.pop(0))

    attempts = 0

    async def broken_callback(_: DittobenchProgressSnapshot) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("telemetry sink unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        report = await DittobenchClient(cast(Any, _poll_config()), http)._poll(
            "private-run-id",
            progress_callback=broken_callback,
            expected_bench_version=2,
        )

    assert report.run_id == "private-run-id"
    assert report.composite == 0.9
    assert attempts == 6


_TRANSCRIPT = b'{"run_id":"private-run-id","cases":[{"case_id":"a","response":{}}]}'


def _done_job_with_transcript(declared: str) -> dict[str, object]:
    job = _done_job()
    job["transcript_sha256"] = declared
    return job


def _transcript_handler(declared: str, body: bytes) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transcript"):
            return httpx.Response(200, content=body)
        return httpx.Response(200, json=_done_job_with_transcript(declared))

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_poll_fetches_transcript_and_binds_digest() -> None:
    import hashlib

    declared = hashlib.sha256(_TRANSCRIPT).hexdigest()
    async with httpx.AsyncClient(
        transport=_transcript_handler(declared, _TRANSCRIPT)
    ) as http:
        client = DittobenchClient(cast(Any, _poll_config()), http)
        report = await client._poll("private-run-id", expected_bench_version=2)

    assert isinstance(report.details, dict)
    assert report.details["transcript_sha256"] == declared
    assert client.last_transcript == _TRANSCRIPT
    assert client.take_transcript("private-run-id") == _TRANSCRIPT
    assert client.take_transcript("private-run-id") is None
    assert client.last_details.get("transcript_sha256") == declared


@pytest.mark.asyncio
async def test_poll_drops_transcript_on_digest_mismatch() -> None:
    async with httpx.AsyncClient(
        transport=_transcript_handler("ab" * 32, _TRANSCRIPT)
    ) as http:
        client = DittobenchClient(cast(Any, _poll_config()), http)
        report = await client._poll("private-run-id", expected_bench_version=2)

    # The score itself never depends on the artifact: the run still parses, but
    # no unverified digest is bound into the report.
    assert report.composite == 0.9
    assert client.last_transcript is None
    assert client.take_transcript("private-run-id") is None
    assert not (report.details or {}).get("transcript_sha256")


@pytest.mark.asyncio
async def test_poll_without_transcript_keeps_legacy_shape() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_done_job())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(cast(Any, _poll_config()), http)
        report = await client._poll("private-run-id", expected_bench_version=2)

    assert report.details == {"bench_version": 2}
    assert client.last_transcript is None
