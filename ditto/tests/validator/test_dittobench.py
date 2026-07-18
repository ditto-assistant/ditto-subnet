"""Tests for the validator's dittobench-api request contract."""

from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any, cast

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
    safe_progress_snapshot,
)
from ditto.validator.errors import DittobenchError, ValidatorInfrastructureError

_REVISION = "ab" * 20


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
    ("status_code", "source_revision", "expected_status", "expected_versions"),
    [
        (200, _REVISION, "fresh_verified", (2, 3)),
        (200, "cd" * 20, "identity_mismatch", (2,)),
        (404, _REVISION, "legacy_v2", (2,)),
        (503, _REVISION, "unreachable", (2,)),
    ],
)
async def test_secretless_scorer_capability_is_provenance_bound(
    status_code: int,
    source_revision: str,
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
                "supported_bench_versions": [2, 3],
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
async def test_v3_uses_versioned_route_and_binds_request() -> None:
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
            bench_version=3,
        )
    assert seen["path"] == "/v2/score"
    assert cast(dict[str, object], seen["body"])["bench_version"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("bench_version", [None, 0, 1, 4])
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


@pytest.mark.parametrize(
    ("status", "stage", "expected_counts"),
    [
        ("queued", "preparing", (None, None)),
        ("building", "building_harness", (None, None)),
        ("generating", "starting_harness", (None, None)),
        ("seeding", "running_benchmark", (None, None)),
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
@pytest.mark.parametrize(("job_version", "report_version"), [(2, 3), (3, 2)])
async def test_v3_poll_rejects_job_or_report_version_mismatch(
    job_version: int, report_version: int
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
            await client._poll("run-v3", expected_bench_version=3)


@pytest.mark.asyncio
async def test_v3_poll_returns_version_bound_report() -> None:
    payload = _done_job()
    payload["bench_version"] = 3
    report = cast(dict[str, object], payload["report"])
    report["details"] = {"bench_version": 3}

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await DittobenchClient(cast(Any, _poll_config()), http)._poll(
            "run-v3", expected_bench_version=3
        )
    assert result.bench_version == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("expected_bench_version", [None, 0, 1, 4])
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
        embed_preflight_url="http://sandbox-docker:11434/api/embed",
        embed_preflight_timeout_seconds=5.0,
    )


@pytest.mark.asyncio
async def test_embedding_preflight_accepts_nonempty_vector() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
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

    def handler(_: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(cast(Any, _preflight_config()), http)
        with pytest.raises(ValidatorInfrastructureError):
            await client.preflight()
        await client.preflight()


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
@pytest.mark.parametrize("code", ["sandbox_oom", "sandbox_tmpfs_exhausted"])
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
    assert seen == [{"stage": "finalizing", "completed": 114, "total": 114}]
    serialized = json.dumps(seen)
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
