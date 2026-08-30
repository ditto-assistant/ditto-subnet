"""The platform must consume the standalone screening protocol package."""

import json
from uuid import UUID

from ditto.api_models.agent_status import AgentStatus as CompatibilityAgentStatus
from ditto.api_models.screener import ScreenResultRequest as CompatibilityRequest
from ditto_screening_protocol import (
    SCREENING_POLICY_VERSION,
    AgentStatus,
    ScreenResultOutcome,
    ScreenResultRequest,
    SubmissionImageBuildResponse,
    SubmissionSourceReviewResponse,
    verdict_signing_message,
)


def test_compatibility_imports_are_shared_package_types() -> None:
    assert CompatibilityAgentStatus is AgentStatus
    assert CompatibilityRequest is ScreenResultRequest


def test_local_hetzner_jobs_are_valid_screening_wire_responses() -> None:
    build_id = UUID("550e8400-e29b-41d4-a716-446655440010")
    attempt_id = UUID("550e8400-e29b-41d4-a716-446655440011")

    build = SubmissionImageBuildResponse(
        build_id=build_id,
        attempt_id=attempt_id,
        status="succeeded",
        provider="hetzner",
        artifact_sha256="12" * 32,
        image_ref=f"ditto-screen/{build_id}-{attempt_id}:latest",
        output_sha256="34" * 32,
        output_size_bytes=1024,
        download_url="https://example.invalid/image.tar.zst",
        runtime_status="succeeded",
        runtime_provider="hetzner",
        runtime_image_reference=(
            "registry.example.invalid/ditto/screener@sha256:" + "56" * 32
        ),
    )
    review = SubmissionSourceReviewResponse(
        review_id=UUID("550e8400-e29b-41d4-a716-446655440012"),
        attempt_id=attempt_id,
        status="running",
        provider="hetzner",
        artifact_sha256="12" * 32,
    )

    assert build.provider == "hetzner"
    assert build.runtime_provider == "hetzner"
    assert review.provider == "hetzner"


def test_canonical_versioned_verdict_message_is_wire_compatible() -> None:
    hotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")
    assert (
        verdict_signing_message(
            screener_hotkey=hotkey,
            agent_id=agent_id,
            passed=True,
            policy_version=8,
        )
        == f"{hotkey}:{agent_id}:True:8".encode()
    )


def test_canonical_lease_bound_verdict_message_is_exact() -> None:
    hotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")
    attempt_id = UUID("550e8400-e29b-41d4-a716-446655440001")
    assert (
        verdict_signing_message(
            screener_hotkey=hotkey,
            agent_id=agent_id,
            attempt_id=attempt_id,
            passed=False,
            policy_version=8,
        )
        == (
            f"ditto-screen-verdict:v2:{hotkey}:{agent_id}:{attempt_id}:False:8"
        ).encode()
    )


def test_canonical_policy_v9_message_binds_upload_identity() -> None:
    hotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")
    attempt_id = UUID("550e8400-e29b-41d4-a716-446655440001")
    upload_id = UUID("550e8400-e29b-41d4-a716-446655440002")

    message = verdict_signing_message(
        screener_hotkey=hotkey,
        agent_id=agent_id,
        attempt_id=attempt_id,
        passed=True,
        policy_version=SCREENING_POLICY_VERSION,
        outcome=ScreenResultOutcome.PASS,
        image_sha256="12" * 32,
        image_size_bytes=123,
        image_id="sha256:" + "34" * 32,
        image_ref=f"ditto-screen/{agent_id}:latest",
        image_upload_id=upload_id,
    )

    payload = json.loads(message.removeprefix(b"ditto-screen-result:v5:").decode())
    assert payload["attempt_id"] == str(attempt_id)
    assert payload["image_upload_id"] == str(upload_id)
    assert payload["image_sha256"] == "12" * 32
