"""Tests for the validator's dittobench-api request contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import httpx
import pytest

from ditto.api_models.validator import (
    LEGACY_FAILURE_DETAIL_MAX_LENGTH,
    FailJobRequest,
    V9BaseEvidence,
)
from ditto.api_models.validator_capabilities import (
    ScorerBenchmarkCapability,
    ValidatorComponentIdentity,
    ValidatorStackComponents,
    ValidatorStackIdentity,
)
from ditto.validator.dittobench import (
    _AGENT_ATTRIBUTABLE_INFERENCE_CODES,
    _SANDBOX_INFRASTRUCTURE_CODES,
    DittobenchClient,
    DittobenchProgressSnapshot,
    InferenceBrokerSession,
    _agent_attributable_failure_code,
    _sandbox_infrastructure_failure_code,
    safe_progress_snapshot,
)
from ditto.validator.errors import (
    FAILURE_DETAIL_MAX_LENGTH,
    DittobenchError,
    SandboxOomError,
    ValidatorInfrastructureError,
    failure_detail,
)
from ditto.validator.signing import score_signing_message

_REVISION = "ab" * 20
_V9_CONTRACT_VECTOR = (
    Path(__file__).resolve().parents[3]
    / "services/dittobench-api/testdata/v9_base_contract_vectors.json"
)


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


def _control_plane_handler(
    expected_token: str, seen: list[tuple[str, str, str | None]]
) -> object:
    """Emulate the scorer's ``controlAuthorized`` for a non-loopback peer.

    dittobench-api admits ``/v1/inference/session*`` when the peer is loopback
    OR presents the configured bearer. The scorer joins sandbox-docker's
    network namespace while the validator stays on the Compose bridge, so in
    production the peer is never loopback: the bearer is the only path. This
    handler therefore refuses loopback outright and answers 401 exactly like
    the scorer does.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("Authorization")
        seen.append((request.method, request.url.path, authorization))
        if authorization != f"Bearer {expected_token}":
            return httpx.Response(
                401, json={"error": "inference control plane unavailable"}
            )
        if request.url.path.endswith("/activate"):
            return httpx.Response(200, json={"active": True})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(
            201,
            json={
                "session_id": "session",
                "activation_secret": "activation",
                "broker_public_key": "public",
            },
        )

    return handler


@pytest.mark.asyncio
async def test_inference_control_plane_authorizes_with_the_scorer_token() -> None:
    """Every control-plane call must carry the shared scorer control token.

    Without it the v7 activation path dies at ``prepare`` with the scorer's 401
    and the ticket is reported failed with ``reason=infrastructure`` — the
    v0.29.5 rollout outage. The regression case below is that exact shape.
    """
    seen: list[tuple[str, str, str | None]] = []
    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_control_token="stack-control-token",
    )
    transport = httpx.MockTransport(
        cast(Any, _control_plane_handler("stack-control-token", seen))
    )
    async with httpx.AsyncClient(transport=transport) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        session = await client.prepare_inference_session()
        await client.activate_inference_session(
            session,
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
        await client.cancel_inference_session(session.session_id)

    assert session.session_id == "session"
    assert [(method, path) for method, path, _ in seen] == [
        ("POST", "/v1/inference/session"),
        ("POST", "/v1/inference/session/session/activate"),
        ("DELETE", "/v1/inference/session/session"),
    ]
    assert {authorization for _, _, authorization in seen} == {
        "Bearer stack-control-token"
    }


@pytest.mark.asyncio
async def test_unconfigured_control_token_is_rejected_by_the_scorer() -> None:
    seen: list[tuple[str, str, str | None]] = []
    config = SimpleNamespace(dittobench_api_url="http://dittobench.test")
    transport = httpx.MockTransport(
        cast(Any, _control_plane_handler("stack-control-token", seen))
    )
    async with httpx.AsyncClient(transport=transport) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        with pytest.raises(
            ValidatorInfrastructureError, match="inference broker preparation rejected"
        ):
            await client.prepare_inference_session()

    assert seen == [("POST", "/v1/inference/session", None)]


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
        # Retired-only scorers remain readable transition input but advertise no
        # runnable work to the validator.
        (200, _REVISION, [2, 3], "unreachable", ()),
        (200, _REVISION, [2, 3, 4], "unreachable", ()),
        (200, _REVISION, [4], "unreachable", ()),
        (200, _REVISION, [2, 3, 4, 5, 6], "unreachable", ()),
        (200, _REVISION, [5], "unreachable", ()),
        (200, _REVISION, [6], "unreachable", ()),
        (200, _REVISION, [2, 3, 4, 5, 6, 7], "unreachable", ()),
        # Rolling upgrades negotiate the common v8 contract without reviving
        # any retired scoring path.
        (
            200,
            _REVISION,
            [2, 3, 4, 5, 6, 7, 8],
            "fresh_verified",
            (8,),
        ),
        # Unknown historical/gap versions remain malformed.
        (200, _REVISION, [1, 2, 3, 4, 5, 6], "unreachable", ()),
        (200, _REVISION, [8], "fresh_verified", (8,)),
        (200, "cd" * 20, [8], "identity_mismatch", ()),
        (404, _REVISION, [2, 3], "unreachable", ()),
        (503, _REVISION, [2, 3], "unreachable", ()),
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
async def test_v044_rolling_upgrade_negotiates_v8_and_preserves_capacity() -> None:
    """A pre-retirement scorer remains usable only through its v8 overlap."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "software_version": "0.29.4",
                "source_revision": _REVISION,
                "supported_bench_versions": [7, 8],
                "full_run_capacity": 6,
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
    assert observed.supported_bench_versions == (8,)
    assert observed.v7_calibration is None
    assert client.full_run_capacity == 6


@pytest.mark.asyncio
async def test_dual_version_scorer_preserves_versions_and_run_capacity() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "software_version": "0.22.0",
                "source_revision": _REVISION,
                "supported_bench_versions": [8, 9],
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
    assert observed.supported_bench_versions == (8, 9)
    assert client.full_run_capacity == 2


# A still-supported v8-only scorer capability shape used for rolling-upgrade tests.
_LIVE_CAPABILITIES: dict[str, Any] = {
    "software_version": "0.29.4",
    "source_revision": "ecfac4cf15fbf46a5965677eaf0a9393d0f06cd8",
    "supported_bench_versions": [8],
    "full_run_capacity": 1,
    "memory_phase_capacity": 1,
    "source_revision_origin": "binary",
    "source_revision_mismatch": False,
    "software_version_origin": "binary",
}


@pytest.mark.asyncio
async def test_live_released_scorer_advertises_only_v8() -> None:

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_LIVE_CAPABILITIES)

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_mock=False,
        dittobench_capabilities_timeout_seconds=1,
        scorer_require_binary_provenance=True,
    )
    revision = _LIVE_CAPABILITIES["source_revision"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        observed = await client.scorer_benchmark_capability(_stack(revision))

    assert observed.status == "fresh_verified"
    assert observed.supported_bench_versions == (8,)
    assert observed.v7_calibration is None
    assert client.scorer_identity_fault is None


@pytest.mark.asyncio
async def test_retired_calibration_metadata_is_ignored() -> None:

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                **_LIVE_CAPABILITIES,
                "v7_calibration": {"manifest_sha256": "not-a-digest"},
            },
        )

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_mock=False,
        dittobench_capabilities_timeout_seconds=1,
        scorer_require_binary_provenance=True,
    )
    revision = _LIVE_CAPABILITIES["source_revision"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        observed = await client.scorer_benchmark_capability(_stack(revision))

    assert observed.status == "fresh_verified"
    assert observed.supported_bench_versions == (8,)
    assert observed.v7_calibration is None


# --- Scorer liveness probe (heartbeat protocol v15) -------------------------
#
# The status above is the conclusion the validator drew. These cover the
# evidence behind it, because the conclusion alone could not tell a dead
# sidecar from an old one, or a narrowed advertisement from a clean success.


def _probe_config() -> SimpleNamespace:
    return SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_mock=False,
        dittobench_capabilities_timeout_seconds=1,
        scorer_require_binary_provenance=True,
    )


async def _observe(
    handler: object, *, revision: str | None = None, times: int = 1
) -> ScorerBenchmarkCapability:
    """Run the capability probe ``times`` times against one handler."""
    revision = revision or cast(str, _LIVE_CAPABILITIES["source_revision"])
    transport = httpx.MockTransport(cast(Any, handler))
    async with httpx.AsyncClient(transport=transport) as http:
        client = DittobenchClient(_probe_config(), http)  # type: ignore[arg-type]
        for _ in range(times):
            observed = await client.scorer_benchmark_capability(_stack(revision))
    return observed


@pytest.mark.asyncio
async def test_a_sidecar_that_404s_is_not_mistaken_for_an_old_scorer() -> None:
    """The TAO.com outage.

    Its dittobench-api answered 404 on ``/v1/capabilities``. The validator must
    report the scorer as unreachable and carry the exact request evidence.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    observed = await _observe(handler, times=3)

    assert observed.status == "unreachable"
    assert observed.probe is not None
    assert observed.probe.outcome == "http_error"
    assert observed.probe.http_status == 404
    assert observed.probe.reason is None
    # It has never once served, and it has now failed three probes running.
    assert observed.probe.last_served_at is None
    assert observed.probe.consecutive_failures == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        pytest.param(
            httpx.ConnectError("connection refused"), "connect_error", id="refused"
        ),
        pytest.param(
            httpx.ReadTimeout("deadline exceeded"), "timeout", id="wedged-and-silent"
        ),
    ],
)
async def test_no_answer_at_all_says_which_kind_of_no_answer(
    error: httpx.HTTPError, outcome: str
) -> None:
    """Different faults, different fixes: nothing listening vs. wedged."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise error

    observed = await _observe(handler)

    assert observed.status == "unreachable"
    assert observed.probe is not None
    assert observed.probe.outcome == outcome
    assert observed.probe.http_status is None
    assert observed.probe.consecutive_failures == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        pytest.param("not json at all", "invalid_json", id="not-json"),
        pytest.param("[]", "malformed_capabilities", id="json-but-not-a-document"),
        pytest.param(
            json.dumps({**_LIVE_CAPABILITIES, "source_revision": "nope"}),
            "malformed_capabilities",
            id="off-pattern-identity",
        ),
        pytest.param(
            json.dumps({**_LIVE_CAPABILITIES, "supported_bench_versions": [1, 2, 3]}),
            "unsupported_bench_version",
            id="unknown-version-in-range",
        ),
        pytest.param(
            json.dumps({**_LIVE_CAPABILITIES, "supported_bench_versions": [0, 8]}),
            "malformed_capabilities",
            id="non-positive-version",
        ),
    ],
)
async def test_an_answer_this_validator_cannot_use_says_why(
    body: str, reason: str
) -> None:
    """A 200 is not evidence of service; being able to read it is."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode())

    observed = await _observe(handler)

    assert observed.status == "unreachable"
    assert observed.probe is not None
    assert observed.probe.outcome == "unreadable"
    assert observed.probe.http_status == 200
    assert observed.probe.reason == reason


@pytest.mark.asyncio
async def test_an_extra_descriptive_field_still_reads_as_fully_served() -> None:
    """Unknown descriptive fields must not narrow the v8 advertisement."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                **_LIVE_CAPABILITIES,
                "measured_at": "2026-07-25T01:32:00Z",
                "provenance": "reviewed-measured",
            },
        )

    observed = await _observe(handler)

    assert observed.status == "fresh_verified"
    assert observed.supported_bench_versions == (8,)
    assert observed.probe is not None
    assert observed.probe.outcome == "served"
    assert observed.probe.reason is None
    assert observed.probe.consecutive_failures == 0
    assert observed.probe.last_served_at == observed.probe.observed_at


@pytest.mark.asyncio
async def test_retired_metadata_does_not_narrow_v8_advertisement() -> None:

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                **_LIVE_CAPABILITIES,
                "v7_calibration": {"manifest_sha256": "not-a-digest"},
            },
        )

    observed = await _observe(handler)

    assert observed.status == "fresh_verified"
    assert observed.supported_bench_versions == (8,)
    assert observed.probe is not None
    assert observed.probe.outcome == "served"
    assert observed.probe.reason is None


@pytest.mark.asyncio
async def test_a_stale_scorer_image_is_named_by_the_probe_too() -> None:
    """An identity mismatch is a scorer that answers and is still the wrong one."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_LIVE_CAPABILITIES)

    observed = await _observe(handler, revision="cd" * 20)

    assert observed.status == "identity_mismatch"
    assert observed.probe is not None
    assert observed.probe.outcome == "served_degraded"
    assert observed.probe.reason == "identity_mismatch"


@pytest.mark.asyncio
async def test_recovery_clears_the_failure_run_and_records_the_service() -> None:
    """An operator watching for the fix needs to see the counter reset."""
    replies = iter([404, 404, 200])

    def handler(_: httpx.Request) -> httpx.Response:
        status = next(replies)
        if status != 200:
            return httpx.Response(status, json={"detail": "Not Found"})
        return httpx.Response(200, json=_LIVE_CAPABILITIES)

    observed = await _observe(handler, times=3)

    assert observed.status == "fresh_verified"
    assert observed.probe is not None
    assert observed.probe.outcome == "served"
    assert observed.probe.consecutive_failures == 0
    assert observed.probe.last_served_at == observed.probe.observed_at


@pytest.mark.asyncio
async def test_mock_mode_reports_that_it_observed_nothing() -> None:
    """Plumbing mode must never manufacture evidence of a healthy scorer."""
    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_mock=True,
        dittobench_capabilities_timeout_seconds=1,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(cast(Any, lambda _: httpx.Response(500)))
    ) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        observed = await client.scorer_benchmark_capability(_stack())

    assert observed.status == "unreachable"
    assert observed.probe is not None
    assert observed.probe.outcome == "not_probed"
    assert observed.probe.http_status is None
    assert observed.probe.last_served_at is None
    assert observed.probe.consecutive_failures == 0


@pytest.mark.asyncio
async def test_v7_calibration_is_not_propagated_into_active_capability() -> None:
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
                "supported_bench_versions": [8],
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

    assert observed.supported_bench_versions == (8,)
    assert observed.v7_calibration is None


_V7_CALIBRATION = {
    "manifest_sha256": "12" * 32,
    "supported_routes": [
        {
            "provider": "wandb",
            "profile_revision": "openrouter-wandb-gpt-oss-20b-v1",
            "model": "openai/gpt-oss-20b",
        }
    ],
}

# What a correctly built scorer at the pinned revision answers: the revision is
# compiled in, and it says so.
_STAMPED = {
    "software_version": "0.29.4",
    "source_revision": _REVISION,
    "source_revision_origin": "binary",
    "source_revision_mismatch": False,
    "supported_bench_versions": [8],
    "v7_calibration": _V7_CALIBRATION,
}


def _capability_client(
    payload: dict[str, Any], *, require_binary_provenance: bool = True
) -> tuple[DittobenchClient, httpx.AsyncClient]:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_mock=False,
        dittobench_capabilities_timeout_seconds=1,
        scorer_require_binary_provenance=require_binary_provenance,
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DittobenchClient(config, http), http  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_environment_asserted_revision_from_a_stamping_pin_is_stale() -> None:
    """The live v0.29.3 incident, in the shape the fleet is stuck in today.

    A container recreated against a cached image gets the new pin's
    ``DITTOBENCH_SOURCE_SHA`` immediately while the old binary keeps running, so
    the revision matches the pin exactly and the old check passed. The pinned
    revision compiles its identity in, so an answer that is only asserted by the
    environment cannot have come from it.
    """
    client, http = _capability_client(
        {
            "software_version": "0.29.3",
            "source_revision": _REVISION,
            "supported_bench_versions": [8],
            "v7_calibration": _V7_CALIBRATION,
        }
    )
    async with http:
        observed = await client.scorer_benchmark_capability(_stack())

    assert observed.status == "identity_mismatch"
    assert observed.supported_bench_versions == ()
    # The claimed identity still travels, so an operator sees exactly what the
    # scorer said about itself.
    assert observed.source_revision == _REVISION
    assert observed.software_version == "0.29.3"
    assert client.scorer_identity_fault == "scorer_image_stale"
    # A scorer that cannot prove what it is never contributes its capacity claim.
    assert client.full_run_capacity == 1


@pytest.mark.asyncio
async def test_explicit_env_provenance_is_stale_for_the_same_reason() -> None:
    """A silently mistyped ``-X`` produces an unstamped binary reporting "env".

    That is indistinguishable from an unrebuilt image and must be treated the
    same way, not waved through because the field was present.
    """
    client, http = _capability_client({**_STAMPED, "source_revision_origin": "env"})
    async with http:
        observed = await client.scorer_benchmark_capability(_stack())

    assert observed.status == "identity_mismatch"
    assert client.scorer_identity_fault == "scorer_image_stale"


@pytest.mark.asyncio
async def test_binary_derived_revision_matching_the_pin_is_verified() -> None:
    client, http = _capability_client({**_STAMPED, "full_run_capacity": 2})
    async with http:
        observed = await client.scorer_benchmark_capability(_stack())

    assert observed.status == "fresh_verified"
    assert observed.supported_bench_versions == (8,)
    assert observed.v7_calibration is None
    assert client.scorer_identity_fault is None
    assert client.full_run_capacity == 2


@pytest.mark.asyncio
async def test_a_pin_that_cannot_stamp_keeps_the_previous_behaviour() -> None:
    """The requirement is committed beside the pin and must move with it.

    A pin whose scorer predates build-time stamping could never satisfy the
    check, so enforcing it there would degrade every validator on the subnet
    over a revision that is in fact correct.
    """
    client, http = _capability_client(
        {
            "software_version": "0.29.3",
            "source_revision": _REVISION,
            "supported_bench_versions": [8],
            "v7_calibration": _V7_CALIBRATION,
        },
        require_binary_provenance=False,
    )
    async with http:
        observed = await client.scorer_benchmark_capability(_stack())

    assert observed.status == "fresh_verified"
    assert observed.supported_bench_versions == (8,)
    assert client.scorer_identity_fault is None


@pytest.mark.asyncio
async def test_self_reported_binary_environment_disagreement_is_stale() -> None:
    """The scorer reports that its binary and its environment disagreed.

    A stale deployment by the scorer's own admission, even though the compiled
    revision it reports matches the pin. ``source_revision_mismatch`` is always
    emitted by a scorer new enough to have an opinion, so ``False`` is never
    ambiguous with an older scorer that omits it.
    """
    client, http = _capability_client({**_STAMPED, "source_revision_mismatch": True})
    async with http:
        observed = await client.scorer_benchmark_capability(_stack())

    assert observed.status == "identity_mismatch"
    assert observed.supported_bench_versions == ()
    assert client.scorer_identity_fault == "scorer_image_stale"


@pytest.mark.asyncio
async def test_plain_revision_disagreement_is_reported_separately() -> None:
    client, http = _capability_client({**_STAMPED, "source_revision": "cd" * 20})
    async with http:
        observed = await client.scorer_benchmark_capability(_stack())

    assert observed.status == "identity_mismatch"
    assert client.scorer_identity_fault == "scorer_revision_mismatch"


@pytest.mark.asyncio
async def test_recovered_scorer_clears_the_reported_fault() -> None:
    """An operator watching for the fix must see the fault clear, not latch."""
    client, stale_http = _capability_client(
        {
            "software_version": "0.29.3",
            "source_revision": _REVISION,
            "supported_bench_versions": [8],
            "v7_calibration": _V7_CALIBRATION,
        }
    )
    async with stale_http:
        stale = await client.scorer_benchmark_capability(_stack())
    assert stale.status == "identity_mismatch"
    assert client.scorer_identity_fault == "scorer_image_stale"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_STAMPED)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as rebuilt:
        client._client = rebuilt
        observed = await client.scorer_benchmark_capability(_stack())

    assert observed.status == "fresh_verified"
    assert client.scorer_identity_fault is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bench_version", [8, 9])
async def test_current_versions_use_versioned_route_and_bind_request(
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
@pytest.mark.parametrize("bench_version", [None, 0, 1, 6, 10])
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
                tarball_url="https://example.test/agent.tgz",
                bench_version=8,
                dataset_sha256="12" * 32,
                screened_image_url="https://example.test/image.tar",
                screened_image_sha256="34" * 32,
                screened_image_size_bytes=123,
                screened_image_id="sha256:" + "56" * 32,
                screened_image_ref=(
                    "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
                ),
            )


@pytest.mark.parametrize(
    ("status", "stage", "expected_counts"),
    [
        ("queued", "preparing", (None, None)),
        ("building", "building_harness", (None, None)),
        ("generating", "generating_dataset", (None, None)),
        ("seeding", "starting_harness", (None, None)),
        ("running", "running_benchmark", (51, 114)),
        ("waiting_for_relay", "waiting_for_relay", (51, 114)),
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
            tarball_url="https://example.test/agent.tgz",
            bench_version=8,
            dataset_sha256="12" * 32,
            screened_image_url="https://example.test/image.tar",
            screened_image_sha256="34" * 32,
            screened_image_size_bytes=123,
            screened_image_id="sha256:" + "56" * 32,
            screened_image_ref=(
                "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
            ),
        )

    assert run_id == "run-1"
    assert request_body == {
        "tarball_url": "https://example.test/agent.tgz",
        "run_size": "full",
        "screened_image_url": "https://example.test/image.tar",
        "screened_image_sha256": "34" * 32,
        "screened_image_size_bytes": 123,
        "screened_image_id": "sha256:" + "56" * 32,
        "screened_image_ref": (
            "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"
        ),
        "dataset_sha256": "12" * 32,
        "bench_version": 8,
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
            bench_version=8,
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
                bench_version=8,
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
            bench_version=8,
            tarball_sha256="ab" * 32,
            dataset_sha256="56" * 32,
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
                bench_version=8,
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
                bench_version=8,
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
            await client._poll("run-1", expected_bench_version=8)

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
            await client._poll("run-1", expected_bench_version=8)


def _done_job() -> dict[str, object]:
    return {
        "status": "done",
        "bench_version": 8,
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
            "details": {"bench_version": 8},
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
    [(8, 7, 8), (8, 8, 7)],
)
async def test_current_poll_rejects_job_or_report_version_mismatch(
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
async def test_current_poll_returns_v8_version_bound_report() -> None:
    bench_version = 8
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
@pytest.mark.parametrize("expected_bench_version", [None, 0, 1, 6, 10])
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
            await client._poll("run-v2", expected_bench_version=8)


@pytest.mark.asyncio
async def test_hosted_embedding_run_failure_is_retryable_infrastructure() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "failed",
                "error": (
                    "seeding haystack wave 2 failed: embedding service unavailable"
                ),
                "failure": {
                    "kind": "validator_infrastructure",
                    "code": "embedding_provider_unavailable",
                    "retryable": True,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValidatorInfrastructureError, match="embedding"):
            await DittobenchClient(cast(Any, _poll_config()), http)._poll(
                "run-1", expected_bench_version=8
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "sandbox_oom",
        "sandbox_tmpfs_exhausted",
        "sandbox_network_unavailable",
        "model_relay_unavailable",
        "embedding_provider_unavailable",
        "screened_image_unavailable",
        "seed_store_lock_timeout",
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
        expected_error = (
            SandboxOomError if code == "sandbox_oom" else ValidatorInfrastructureError
        )
        expected_message = "memory allowance" if code == "sandbox_oom" else code
        with pytest.raises(expected_error, match=expected_message) as caught:
            await DittobenchClient(cast(Any, _poll_config()), http)._poll(
                "run-1", expected_bench_version=8
            )
    assert private_marker not in str(caught.value)
    # The scorer's own classifier survives the raise. All five codes collapse
    # into one `infrastructure` hand-back, so without this the specific code
    # existed only in a validator-host log line -- which is why ditto-subnet#279
    # could not name what killed a `mnemo*` run at ~60 minutes. It now rides the
    # wire as `failure_detail` and lands on the ticket.
    assert caught.value.code == code
    assert failure_detail(caught.value) == code


@pytest.mark.asyncio
async def test_relay_recovery_exhaustion_reaches_failure_detail() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "failed",
                "error": "private relay error",
                "failure": {
                    "kind": "validator_infrastructure",
                    "code": "model_relay_unavailable",
                    "retryable": True,
                    "diagnostics": {"relay_cause": "provider_recovery_exhausted"},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValidatorInfrastructureError) as caught:
            await DittobenchClient(cast(Any, _poll_config()), http)._poll(
                "run-1", expected_bench_version=8
            )
    assert caught.value.code == ("model_relay_unavailable:provider_recovery_exhausted")
    assert failure_detail(caught.value) == caught.value.code


@pytest.mark.asyncio
async def test_unchanged_progress_watchdog_cancels_despite_heartbeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 0.0
    methods: list[str] = []
    heartbeats: list[DittobenchProgressSnapshot] = []

    async def advance(_seconds: float) -> None:
        nonlocal clock
        clock += 300.0

    async def heartbeat(snapshot: DittobenchProgressSnapshot) -> None:
        heartbeats.append(snapshot)

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "DELETE":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "status": "running",
                "progress": {"done": 12, "total": 114},
            },
        )

    monkeypatch.setattr("ditto.validator.dittobench.time.monotonic", lambda: clock)
    monkeypatch.setattr("ditto.validator.dittobench.asyncio.sleep", advance)
    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_timeout_seconds=5_000.0,
        dittobench_poll_seconds=300.0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValidatorInfrastructureError) as caught:
            await DittobenchClient(cast(Any, config), http)._poll(
                "stalled-run",
                expected_bench_version=8,
                progress_callback=heartbeat,
            )

    assert caught.value.code == "scorer_progress_stalled"
    assert len(heartbeats) == 4
    assert methods == ["GET", "GET", "GET", "GET", "DELETE"]


@pytest.mark.asyncio
async def test_real_count_progress_resets_watchdog_and_done_wins_boundary_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 0.0
    methods: list[str] = []
    payloads = [
        {"status": "running", "progress": {"done": 0, "total": 114}},
        {"status": "running", "progress": {"done": 1, "total": 114}},
        _done_job(),
    ]

    async def advance(_seconds: float) -> None:
        nonlocal clock
        clock += 600.0

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json=payloads.pop(0))

    monkeypatch.setattr("ditto.validator.dittobench.time.monotonic", lambda: clock)
    monkeypatch.setattr("ditto.validator.dittobench.asyncio.sleep", advance)
    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_timeout_seconds=5_000.0,
        dittobench_poll_seconds=600.0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        report = await DittobenchClient(cast(Any, config), http)._poll(
            "progressing-run", expected_bench_version=8
        )

    assert report.composite == 0.9
    assert methods == ["GET", "GET", "GET"]


def test_failure_detail_falls_back_to_the_exception_when_uncoded() -> None:
    # An unclassified failure still names itself, which beats a three-value
    # class and nothing else.
    assert (
        failure_detail(DittobenchError("run r-1 failed: boom"))
        == "DittobenchError: run r-1 failed: boom"
    )


def _fail_job_body(detail: str) -> dict[str, str]:
    return {
        "validator_hotkey": "5" + "A" * 47,
        "agent_id": "550e8400-e29b-41d4-a716-446655440000",
        "ticket_deadline": "2026-07-14T12:30:00Z",
        "reason": "scoring_error",
        "failure_detail": detail,
        "nonce": "550e8400-e29b-41d4-a716-446655440001",
        "requested_at": "2026-07-14T12:00:00Z",
        "signature": "ab" * 64,
    }


def test_failure_detail_is_truncated_to_the_wire_cap() -> None:
    # Enforced on the sending side, not merely respected: an overlong detail
    # would be rejected 422 and lose the whole hand-back, turning a diagnosis
    # into the silent expiry the field exists to prevent.
    detail = failure_detail(DittobenchError("x" * 50_000))
    assert len(detail) == FAILURE_DETAIL_MAX_LENGTH
    assert FailJobRequest.model_fields["failure_detail"].default is None
    parsed = FailJobRequest.model_validate(_fail_job_body(detail))
    assert parsed.failure_detail == detail


def test_truncation_announces_itself_instead_of_cutting_silently() -> None:
    # The old cap cut mid-word and said nothing, so the fragment it left behind
    # read as a finished sentence. That is what made 200 chars expensive: not
    # the lost characters but the absence of any sign that characters were lost.
    # A truncated detail must now be recognizable as truncated, and must still
    # fit the wire bound *with* the marker on it -- a marker that pushed the
    # value back over the cap would 422 the hand-back it exists to preserve.
    detail = failure_detail(DittobenchError("x" * 50_000))
    assert len(detail) == FAILURE_DETAIL_MAX_LENGTH
    assert detail.endswith("chars]")
    assert "...[truncated," in detail
    # The pre-truncation length is recorded, because "how much is missing" is
    # the next question and it is unrecoverable after the fact. 50_000 x's plus
    # the "DittobenchError: " prefix.
    assert str(50_000 + len("DittobenchError: ")) in detail
    assert (
        FailJobRequest.model_validate(_fail_job_body(detail)).failure_detail == detail
    )


def test_a_detail_that_fits_gets_no_truncation_marker() -> None:
    # The overwhelmingly common case. A whole message must not acquire a marker
    # it has not earned -- a reader who sees one goes looking for a missing
    # clause, and sending them after nothing is its own kind of noise.
    detail = failure_detail(DittobenchError("run r-1 failed: boom"))
    assert detail == "DittobenchError: run r-1 failed: boom"
    assert "truncated" not in detail


def test_the_real_inference_decline_message_survives_the_round_trip() -> None:
    # The regression this widening exists for, asserted end to end.
    #
    # This is the actual 2026-07-27 production value -- the message that named a
    # root cause three separate investigations had failed to reach. At the old
    # 200-char bound it arrived cut at "inference r", discarding "equest(s)
    # outright, before reserving any capacity": the clause saying what the
    # platform had done. The count (81) and the verb (rejected) survived by pure
    # luck of word order; a sentence built slightly differently would have lost
    # both and left a fragment that still read as complete.
    error = DittobenchError(
        "run 2b7c6b6c-ae45-493d-b8f5-b1a4a6ff8b3a failed: harness exhausted its "
        "inference allowance: agent-attributable inference decline: the platform "
        "rejected 81 of the harness's inference request(s) outright, before "
        "reserving any capacity"
    )
    detail = failure_detail(error)

    # Longer than the bound that truncated it, and now carried whole.
    assert len(detail) > LEGACY_FAILURE_DETAIL_MAX_LENGTH
    assert "truncated" not in detail
    assert detail == f"DittobenchError: {error}"
    # The clause the old cap ate, named explicitly: a future re-narrowing fails
    # on the thing that was actually lost rather than on a length mismatch.
    assert detail.endswith("outright, before reserving any capacity")
    assert "rejected 81 of the harness's inference request(s)" in detail

    # ...and it validates against the real wire model, which is where the old
    # bound was enforced and where a one-sided revert would show up.
    parsed = FailJobRequest.model_validate(_fail_job_body(detail))
    assert parsed.failure_detail == detail


def test_a_structured_code_still_wins_over_the_message() -> None:
    # Precedence is load bearing and independent of the cap. The code is what an
    # operator groups by and what the classifier reads; the message is the
    # human-facing detail. Widening the bound must not tempt this into
    # preferring the longer, richer-looking string -- a coded error still
    # reports its code alone, exactly as before.
    error = DittobenchError("run r-1 failed: " + "x" * 5000, code="sandbox_oom")
    assert failure_detail(error) == "sandbox_oom"
    assert "truncated" not in failure_detail(error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        # What dittobench-api actually sends for an agent-attributable decline.
        {
            "kind": "sandbox_failure",
            "code": "inference_allowance_exhausted",
            "retryable": False,
        },
        # A healthy broker observed no authoritative chat request at all.
        {
            "kind": "sandbox_failure",
            "code": "model_inference_required",
            "retryable": False,
        },
        # The same code smuggled under the no-fault KIND. Rejected because the
        # code is not in the allowlist.
        {
            "kind": "validator_infrastructure",
            "code": "inference_allowance_exhausted",
            "retryable": True,
        },
    ],
)
async def test_agent_attributable_inference_failures_stay_the_agents(
    failure: dict[str, object],
) -> None:
    """Terminal agent inference outcomes never become no-fault retries.

    ``inference_allowance_exhausted`` means the harness spent the request-count
    or token allowance its own ticket granted, or sent one request too large to
    reserve (platform decline codes 4102/4104/4109). The lease was alive and the
    platform healthy throughout. ``model_inference_required`` means the broker
    was healthy but the harness made no authoritative chat request.

    ``infrastructure`` mints a retry grant, RAISES the attempt cap, and
    re-leases, so an agent that reliably exhausts its own allowance would
    re-lease itself forever -- the mnemox loop, arriving through the inference
    lane instead of the sandbox. Both spellings below must land on
    ``scoring_error``, so the guarantee holds even if a future scorer sends the
    wrong envelope for this code.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "failed",
                "error": "harness exhausted its inference allowance",
                "failure": failure,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DittobenchError) as raised:
            await DittobenchClient(cast(Any, _poll_config()), http)._poll(
                "run-1", expected_bench_version=8
            )
        assert not isinstance(raised.value, ValidatorInfrastructureError)
        expected_code = (
            str(failure["code"]) if failure["kind"] == "sandbox_failure" else None
        )
        assert raised.value.code == expected_code
        if expected_code is not None:
            assert failure_detail(raised.value) == expected_code
        if expected_code == "model_inference_required":
            assert "made no authoritative model call" in str(raised.value)
            assert "exhausted" not in str(raised.value)


def test_agent_inference_codes_are_never_no_fault() -> None:
    """Pinned as a set property, not only through the poll path.

    A future edit adding one of these to ``_SANDBOX_INFRASTRUCTURE_CODES``
    -- the natural mistake, since the neighbouring ``inference_lane_saturated``
    IS in it -- must fail here rather than in production as an unbounded
    re-lease loop.
    """
    assert not (_AGENT_ATTRIBUTABLE_INFERENCE_CODES & _SANDBOX_INFRASTRUCTURE_CODES)
    for code in _AGENT_ATTRIBUTABLE_INFERENCE_CODES:
        assert (
            _agent_attributable_failure_code(
                {
                    "failure": {
                        "kind": "sandbox_failure",
                        "code": code,
                        "retryable": False,
                    }
                }
            )
            == code
        )
        assert (
            _sandbox_infrastructure_failure_code(
                {
                    "failure": {
                        "kind": "validator_infrastructure",
                        "code": code,
                        "retryable": True,
                    }
                }
            )
            is None
        )
    # The complement: lane saturation is deliberately kept no-fault, because a
    # miner can neither provision the platform's inference lane nor see its
    # contention. It reaches us as ``model_relay_unavailable`` with the cause in
    # diagnostics, NOT as a code of its own -- a new no-fault code would be
    # scoring_error on every validator that predates it, which would charge
    # miners for a saturated platform rail for the length of the rollout.
    assert "inference_lane_saturated" not in _SANDBOX_INFRASTRUCTURE_CODES
    assert "model_relay_unavailable" in _SANDBOX_INFRASTRUCTURE_CODES


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
                "run-1", expected_bench_version=8
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        "build failed: screened image sha256 mismatch: got deadbeef",
        "build failed: screened image size mismatch: expected 100 got 99",
        "build failed: screened image id mismatch: expected sha256:aa got sha256:bb",
        "build failed: screened image fetch: unexpected status 404",
        "build failed: invalid screened image archive: manifest.json is missing",
        "build failed: docker image load failed: exit status 1",
        # The no-fault code's own words, in a harness's own output. Only the
        # scorer's typed sentinel mints the code; prose in ``error`` cannot.
        "harness never became healthy: screened image unavailable",
    ],
)
async def test_screened_image_verification_failure_stays_the_agents(
    error: str,
) -> None:
    """The bound on ``screened_image_unavailable``: only acquisition is no-fault.

    ``infrastructure`` mints a retry grant, RAISES the attempt cap, and
    re-leases, so a deterministic failure reaching it would re-lease the same
    permanently broken image forever. dittobench-api keeps every verification
    failure on the terminal ``sandbox_failure``/``retryable=false`` default;
    assert this side agrees, so the two halves of the guarantee stay pinned
    together across repos.
    """

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "failed",
                "error": error,
                "failure": {
                    "kind": "sandbox_failure",
                    "code": "sandbox_runtime",
                    "retryable": False,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DittobenchError):
            await DittobenchClient(cast(Any, _poll_config()), http)._poll(
                "run-1", expected_bench_version=8
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
            "private-run-id", progress_callback=progress, expected_bench_version=8
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
            expected_bench_version=8,
        )

    assert report.run_id == "private-run-id"
    assert report.composite == 0.9
    assert attempts == 6


_TRANSCRIPT = b'{"run_id":"private-run-id","cases":[{"case_id":"a","response":{}}]}'


def _done_job_with_transcript(declared: str) -> dict[str, object]:
    job = _done_job()
    job["transcript_sha256"] = declared
    return job


def _done_v9_job(*, declare_transcript: bool = True) -> dict[str, object]:
    declared = hashlib.sha256(_TRANSCRIPT).hexdigest()
    vector = json.loads(_V9_CONTRACT_VECTOR.read_text())["vectors"][0]
    evidence = vector["details"]
    evidence["run_id"] = "private-run-id"
    evidence["transcript_sha256"] = declared
    validated = V9BaseEvidence.model_validate(evidence)

    job = _done_job()
    job["bench_version"] = 9
    if declare_transcript:
        job["transcript_sha256"] = declared
    report = cast(dict[str, object], job["report"])
    report.update(
        {
            "composite": validated.effective_composite_micros / 1_000_000,
            "composite_stderr": (validated.effective_stderr_micros / 1_000_000),
            "base_evidence_sha256": validated.digest_hex(),
            "details": {
                "bench_version": 9,
                "dataset_sha256": validated.dataset_sha256,
                "transcript_sha256": declared,
                "v9_base": evidence,
            },
        }
    )
    return job


def _transcript_handler(declared: str, body: bytes) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transcript"):
            return httpx.Response(200, content=body)
        return httpx.Response(200, json=_done_job_with_transcript(declared))

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_poll_fetches_transcript_and_binds_digest() -> None:
    declared = hashlib.sha256(_TRANSCRIPT).hexdigest()
    async with httpx.AsyncClient(
        transport=_transcript_handler(declared, _TRANSCRIPT)
    ) as http:
        client = DittobenchClient(cast(Any, _poll_config()), http)
        report = await client._poll("private-run-id", expected_bench_version=8)

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
        report = await client._poll("private-run-id", expected_bench_version=8)

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
        report = await client._poll("private-run-id", expected_bench_version=8)

    assert report.details == {"bench_version": 8}
    assert client.last_transcript is None


@pytest.mark.asyncio
async def test_v8_transcript_fetch_failure_remains_best_effort() -> None:
    declared = hashlib.sha256(_TRANSCRIPT).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transcript"):
            return httpx.Response(503)
        return httpx.Response(200, json=_done_job_with_transcript(declared))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(cast(Any, _poll_config()), http)
        report = await client._poll("private-run-id", expected_bench_version=8)

    assert report.composite == 0.9
    assert report.bench_version == 8
    assert client.last_transcript is None
    assert not (report.details or {}).get("transcript_sha256")


@pytest.mark.asyncio
async def test_v9_poll_validates_evidence_before_returning_signable_report() -> None:
    declared = hashlib.sha256(_TRANSCRIPT).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transcript"):
            return httpx.Response(200, content=_TRANSCRIPT)
        return httpx.Response(200, json=_done_v9_job())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(cast(Any, _poll_config()), http)
        report = await client._poll("private-run-id", expected_bench_version=9)

    assert report.bench_version == 9
    assert report.base_evidence_sha256
    assert report.details and report.details["transcript_sha256"] == declared
    assert client.take_transcript("private-run-id") == _TRANSCRIPT
    message = score_signing_message(
        validator_hotkey="5Fvalidator",
        agent_id=UUID("11111111-2222-3333-4444-555555555555"),
        run_id=report.run_id,
        composite=report.composite,
        seed=report.seed,
        bench_version=report.bench_version,
        transcript_sha256=report.details["transcript_sha256"],
        base_evidence_sha256=report.base_evidence_sha256,
    )
    assert message.endswith(f":{declared}:{report.base_evidence_sha256}".encode())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["undeclared", "rejected", "mismatch"])
async def test_v9_poll_rejects_unavailable_transcript_evidence(failure: str) -> None:
    payload = _done_v9_job(declare_transcript=failure != "undeclared")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transcript"):
            if failure == "rejected":
                return httpx.Response(503)
            body = b"tampered" if failure == "mismatch" else _TRANSCRIPT
            return httpx.Response(200, content=body)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(cast(Any, _poll_config()), http)
        with pytest.raises(
            ValidatorInfrastructureError,
            match="benchmark v9 transcript evidence unavailable",
        ):
            await client._poll("private-run-id", expected_bench_version=9)

    assert client.last_transcript is None
    assert client.take_transcript("private-run-id") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "digest_mismatch"])
async def test_v9_poll_rejects_invalid_base_evidence(failure: str) -> None:
    payload = _done_v9_job()
    report = cast(dict[str, object], payload["report"])
    details = cast(dict[str, object], report["details"])
    if failure == "missing":
        report.pop("base_evidence_sha256")
        details.pop("v9_base")
    else:
        report["base_evidence_sha256"] = "00" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transcript"):
            return httpx.Response(200, content=_TRANSCRIPT)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(cast(Any, _poll_config()), http)
        with pytest.raises(
            ValidatorInfrastructureError,
            match="benchmark v9 base evidence unavailable",
        ):
            await client._poll("private-run-id", expected_bench_version=9)

    assert client.last_transcript is None
    assert client.take_transcript("private-run-id") is None
