"""Tests for screener verdict signing (message format + delegation)."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from ditto_screener.heartbeat import (
    DockerHealth,
    HostSpecs,
    ReviewSettingsStatus,
    ScreenerProgress,
    SystemMetrics,
)
from ditto_screener.signing import (
    heartbeat_signing_message,
    sign_verdict,
    verdict_signing_message,
)
from ditto_screening_protocol import SCREENING_POLICY_VERSION, ScreenResultOutcome

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_AGENT = UUID("550e8400-e29b-41d4-a716-446655440000")
_ATTEMPT = UUID("776a3bb8-5847-40db-b2af-42f93f20233c")


def test_message_matches_platform_format() -> None:
    # Must byte-for-byte match the platform's
    # f"{screener_hotkey}:{agent_id}:{passed}".encode() — including Python's
    # bool str form ("True"/"False").
    msg = verdict_signing_message(
        screener_hotkey=_HOTKEY, agent_id=_AGENT, passed=True, policy_version=8
    )
    assert msg == f"{_HOTKEY}:{_AGENT}:True:8".encode()

    msg_false = verdict_signing_message(
        screener_hotkey=_HOTKEY, agent_id=_AGENT, passed=False, policy_version=8
    )
    assert msg_false.endswith(b":False:8")


class _FakeKeypair:
    """Records what it was asked to sign; returns deterministic bytes."""

    def __init__(self) -> None:
        self.signed: bytes | None = None

    def sign(self, message: bytes) -> bytes:
        self.signed = message
        return b"\xab" * 64


def test_sign_verdict_signs_canonical_message() -> None:
    kp = _FakeKeypair()
    sig = sign_verdict(
        kp,
        screener_hotkey=_HOTKEY,
        agent_id=_AGENT,
        passed=False,
        policy_version=8,
    )
    assert sig == ("ab" * 64)
    assert kp.signed == f"{_HOTKEY}:{_AGENT}:False:8".encode()


def test_attempt_signature_binds_exact_lease() -> None:
    kp = _FakeKeypair()
    sign_verdict(
        kp,
        screener_hotkey=_HOTKEY,
        agent_id=_AGENT,
        attempt_id=_ATTEMPT,
        passed=True,
        policy_version=8,
    )
    assert (
        kp.signed
        == (f"ditto-screen-verdict:v2:{_HOTKEY}:{_AGENT}:{_ATTEMPT}:True:8").encode()
    )


def test_typed_quarantine_signature_binds_private_evidence_digests() -> None:
    message = verdict_signing_message(
        screener_hotkey=_HOTKEY,
        agent_id=_AGENT,
        attempt_id=_ATTEMPT,
        passed=False,
        outcome=ScreenResultOutcome.QUARANTINE,
        manifest_digest="12" * 32,
        finding_digest="34" * 32,
        reason_code="agentic-source-review-tripwire",
        review_settings_revision=42,
        review_settings_instance_id="ditto-screener-prod",
        review_settings_scope="*",
        review_settings_checksum="56" * 32,
    )
    payload = json.loads(message.removeprefix(b"ditto-screen-result:v5:").decode())
    assert payload == {
        "agent_id": str(_AGENT),
        "attempt_id": str(_ATTEMPT),
        "finding_digest": "34" * 32,
        "image_id": None,
        "image_ref": None,
        "image_sha256": None,
        "image_size_bytes": None,
        "image_upload_id": None,
        "manifest_digest": "12" * 32,
        "outcome": "quarantine",
        "policy_version": SCREENING_POLICY_VERSION,
        "reason_code": "agentic-source-review-tripwire",
        "review_settings_checksum": "56" * 32,
        "review_settings_instance_id": "ditto-screener-prod",
        "review_settings_revision": 42,
        "review_settings_scope": "*",
        "screener_hotkey": _HOTKEY,
    }


def test_deferred_source_review_is_signed_only_when_true() -> None:
    base = {
        "screener_hotkey": _HOTKEY,
        "agent_id": _AGENT,
        "attempt_id": _ATTEMPT,
        "passed": False,
        "outcome": ScreenResultOutcome.QUARANTINE,
        "manifest_digest": "12" * 32,
        "reason_code": "behavioral-oracle-failed",
    }
    legacy = verdict_signing_message(**base)
    deferred = verdict_signing_message(**base, deferred_source_review=True)
    legacy_payload = json.loads(
        legacy.removeprefix(b"ditto-screen-result:v5:").decode()
    )
    deferred_payload = json.loads(
        deferred.removeprefix(b"ditto-screen-result:v5:").decode()
    )
    assert "deferred_source_review" not in legacy_payload
    assert deferred_payload["deferred_source_review"] is True
    assert deferred != legacy


def test_final_adjudication_digest_is_bound_into_typed_verdict() -> None:
    base = {
        "screener_hotkey": _HOTKEY,
        "agent_id": _AGENT,
        "attempt_id": _ATTEMPT,
        "passed": False,
        "outcome": ScreenResultOutcome.QUARANTINE,
        "manifest_digest": "12" * 32,
        "reason_code": "source-review-adjudicated",
    }
    without_court = verdict_signing_message(**base)
    with_court = verdict_signing_message(**base, adjudication_digest="ab" * 32)

    payload = json.loads(with_court.removeprefix(b"ditto-screen-result:v5:").decode())
    assert payload["adjudication_digest"] == "ab" * 32
    assert with_court != without_court


def test_source_review_note_ledger_digest_is_bound_into_typed_verdict() -> None:
    base = {
        "screener_hotkey": _HOTKEY,
        "agent_id": _AGENT,
        "attempt_id": _ATTEMPT,
        "passed": True,
        "outcome": ScreenResultOutcome.PASS,
    }
    legacy = verdict_signing_message(**base)
    with_notes = verdict_signing_message(**base, review_notes_digest="ab" * 32)

    payload = json.loads(with_notes.removeprefix(b"ditto-screen-result:v5:").decode())
    assert payload["review_notes_digest"] == "ab" * 32
    assert with_notes != legacy


@pytest.mark.parametrize("policy_version", [9, SCREENING_POLICY_VERSION])
def test_policy_v9_and_later_signature_requires_typed_outcome(
    policy_version: int,
) -> None:
    with pytest.raises(ValueError, match="requires typed outcome"):
        verdict_signing_message(
            screener_hotkey=_HOTKEY,
            agent_id=_AGENT,
            passed=True,
            policy_version=policy_version,
        )


def test_typed_signature_without_binding_remains_rolling_deploy_compatible() -> None:
    message = verdict_signing_message(
        screener_hotkey=_HOTKEY,
        agent_id=_AGENT,
        attempt_id=_ATTEMPT,
        passed=False,
        outcome=ScreenResultOutcome.RETRYABLE_INFRA,
    )
    payload = json.loads(message.removeprefix(b"ditto-screen-result:v5:").decode())
    assert not any(key.startswith("review_settings_") for key in payload)


def test_heartbeat_signature_binds_allowlisted_coarse_metrics() -> None:
    metrics = SystemMetrics(
        collected_at=123,
        cpu_percent=15,
        memory_percent=40,
        disk_percent=55,
        docker=DockerHealth(
            status="healthy", running_containers=3, unhealthy_containers=0
        ),
    )
    assert (
        heartbeat_signing_message(
            screener_hotkey=_HOTKEY,
            software_version="0.1.0",
            protocol_version=1,
            policy_version=6,
            state="screening",
            active_agent_id=_AGENT,
            system_metrics=metrics,
            timestamp=456,
        )
        == (
            "ditto-screener-heartbeat:v1:"
            f"{_HOTKEY}:0.1.0:1:6:screening:{_AGENT}:"
            "123,15,40,55,healthy,3,0:456"
        ).encode()
    )


@pytest.mark.parametrize(
    "stage",
    [
        "preparing",
        "downloading",
        "validating",
        "building",
        "starting",
        "health_check",
        "submitting",
    ],
)
def test_v2_heartbeat_signature_binds_each_stage_and_job_start(stage: str) -> None:
    progress = ScreenerProgress(stage=stage, started_at=400)
    assert (
        heartbeat_signing_message(
            screener_hotkey=_HOTKEY,
            software_version="0.2.0",
            protocol_version=2,
            policy_version=6,
            state="screening",
            active_agent_id=_AGENT,
            progress=progress,
            system_metrics=None,
            timestamp=456,
        )
        == (
            "ditto-screener-heartbeat:v2:"
            f"{_HOTKEY}:0.2.0:2:6:screening:{_AGENT}:{stage},400:-:456"
        ).encode()
    )


def test_v4_heartbeat_binds_applied_review_settings() -> None:
    review = ReviewSettingsStatus(
        revision=42,
        scope="ditto-screener-prod",
        mode="shadow",
        checksum="ab" * 32,
        source="platform",
    )
    assert (
        heartbeat_signing_message(
            screener_hotkey=_HOTKEY,
            software_version="0.14.1",
            protocol_version=4,
            policy_version=9,
            state="polling",
            active_agent_id=None,
            instance_id="ditto-screener-prod",
            progress=None,
            system_metrics=None,
            review_settings=review,
            timestamp=456,
        )
        == (
            "ditto-screener-heartbeat:v4:"
            f"{_HOTKEY}:0.14.1:4:9:polling::ditto-screener-prod:-:-:"
            f"42,ditto-screener-prod,shadow,{'ab' * 32},platform:456"
        ).encode()
    )


def test_v5_heartbeat_binds_policy_manifest_identity() -> None:
    review = ReviewSettingsStatus(
        revision=43,
        scope="*",
        mode="enforce",
        checksum="ab" * 32,
        source="platform",
        policy_manifest_profile="l1_l2",
        policy_manifest_rotation_id="incident-2026-08-27",
        policy_manifest_digest="cd" * 32,
    )
    message = heartbeat_signing_message(
        screener_hotkey=_HOTKEY,
        software_version="0.15.0",
        protocol_version=5,
        policy_version=10,
        state="polling",
        active_agent_id=None,
        instance_id="ditto-screener-prod",
        progress=None,
        system_metrics=None,
        review_settings=review,
        timestamp=456,
    )
    assert f",l1_l2,incident-2026-08-27,{'cd' * 32}:456".encode() in message


def _v5_review_settings() -> ReviewSettingsStatus:
    return ReviewSettingsStatus(
        revision=43,
        scope="*",
        mode="enforce",
        checksum="ab" * 32,
        source="platform",
        policy_manifest_profile="l1_l2",
        policy_manifest_rotation_id="incident-2026-08-27",
        policy_manifest_digest="cd" * 32,
    )


def test_v6_heartbeat_binds_announced_host_specs() -> None:
    message = heartbeat_signing_message(
        screener_hotkey=_HOTKEY,
        software_version="0.16.0",
        protocol_version=6,
        policy_version=11,
        state="polling",
        active_agent_id=None,
        instance_id="ditto-screener-prod",
        progress=None,
        system_metrics=None,
        review_settings=_v5_review_settings(),
        host_specs=HostSpecs(
            cpu_count=16,
            cpu_physical_cores=8,
            memory_total_mib=64000,
            disk_total_gib=500,
            architecture="x86_64",
        ),
        timestamp=456,
    )
    assert message.endswith(b":16,8,64000,500,x86_64:456")


def test_v6_absent_physical_core_count_is_still_unambiguous() -> None:
    message = heartbeat_signing_message(
        screener_hotkey=_HOTKEY,
        software_version="0.16.0",
        protocol_version=6,
        policy_version=11,
        state="polling",
        active_agent_id=None,
        instance_id="ditto-screener-prod",
        progress=None,
        system_metrics=None,
        review_settings=_v5_review_settings(),
        host_specs=HostSpecs(
            cpu_count=4,
            memory_total_mib=8000,
            disk_total_gib=80,
            architecture="aarch64",
        ),
        timestamp=456,
    )
    assert message.endswith(b":4,-,8000,80,aarch64:456")


def test_v6_refuses_to_sign_without_the_specs_it_promises() -> None:
    with pytest.raises(ValueError, match="requires host specs"):
        heartbeat_signing_message(
            screener_hotkey=_HOTKEY,
            software_version="0.16.0",
            protocol_version=6,
            policy_version=11,
            state="polling",
            active_agent_id=None,
            instance_id="ditto-screener-prod",
            progress=None,
            system_metrics=None,
            review_settings=_v5_review_settings(),
            host_specs=None,
            timestamp=456,
        )


def test_v5_bytes_are_unchanged_by_the_v6_extension() -> None:
    """A v6-capable platform must still verify a v5 worker byte-for-byte."""
    message = heartbeat_signing_message(
        screener_hotkey=_HOTKEY,
        software_version="0.15.0",
        protocol_version=5,
        policy_version=10,
        state="polling",
        active_agent_id=None,
        instance_id="ditto-screener-prod",
        progress=None,
        system_metrics=None,
        review_settings=_v5_review_settings(),
        host_specs=HostSpecs(
            cpu_count=16,
            memory_total_mib=64000,
            disk_total_gib=500,
            architecture="x86_64",
        ),
        timestamp=456,
    )
    review_token = (
        f"43,*,enforce,{'ab' * 32},platform,l1_l2,incident-2026-08-27,{'cd' * 32}"
    )
    assert (
        message
        == (
            "ditto-screener-heartbeat:v4:"
            f"{_HOTKEY}:0.15.0:5:10:polling::ditto-screener-prod:-:-:"
            f"{review_token}:456"
        ).encode()
    )
