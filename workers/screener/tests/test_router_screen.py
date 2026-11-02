"""Tests for the shadow router-track source-screen orchestration seam.

``build_signed_router_source_screen`` is the one production call the screener
worker makes: it ties the pure ``screen_router_submission`` to the screener's
hotkey signature. These tests pin the shadow invariants (never weight-eligible,
benign default, graded when a sample is present) and that the signature is bound
to the canonical, content-addressed evidence.
"""

from __future__ import annotations

from ditto_screener.router_screen import (
    ROUTER_SOURCE_ANALYZER_VERSION,
    build_signed_router_source_screen,
)
from ditto_screening_protocol import RouterGeneralizationSample
from ditto_screening_protocol.router_source_screen import (
    RouterSourceScreenOutcome,
    router_source_screen_signing_message,
)

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_ARTIFACT = "aa" * 32
_IMAGE = "bb" * 32


class _FakeKeypair:
    def __init__(self) -> None:
        self.signed: bytes | None = None

    def sign(self, message: bytes) -> bytes:
        self.signed = message
        return b"\xcd" * 64


def test_no_router_project_is_benign_infrastructure_and_shadow() -> None:
    kp = _FakeKeypair()
    evidence, signature = build_signed_router_source_screen(
        keypair=kp,
        screener_hotkey=_HOTKEY,
        agent_artifact_sha256=_ARTIFACT,
        screened_image_sha256=_IMAGE,
        policy_version=1,
        sample=None,
    )
    # Opt-in / yes-and default: no project -> benign, no findings, never a deny.
    assert evidence.outcome is RouterSourceScreenOutcome.INFRASTRUCTURE
    assert evidence.findings == ()
    # Shadow invariant is structural, not a runtime flag.
    assert evidence.weight_eligible is False
    assert evidence.analyzer_version == ROUTER_SOURCE_ANALYZER_VERSION
    assert signature == ("cd" * 64)
    assert kp.signed == router_source_screen_signing_message(
        screener_hotkey=_HOTKEY, evidence=evidence
    )


def test_included_sample_is_graded_and_can_deny() -> None:
    kp = _FakeKeypair()
    # A public arm far above a floored held-out arm is the classic "works only on
    # the public set" signature -> a deterministic deny finding.
    sample = RouterGeneralizationSample(
        public_score_bps=9_000,
        heldout_score_bps=1_000,
        heldout_cases=20,
        distinct_heldout_routes=6,
    )
    evidence, _ = build_signed_router_source_screen(
        keypair=kp,
        screener_hotkey=_HOTKEY,
        agent_artifact_sha256=_ARTIFACT,
        screened_image_sha256=_IMAGE,
        policy_version=1,
        sample=sample,
    )
    assert evidence.outcome is RouterSourceScreenOutcome.DENY
    assert evidence.findings  # carries the generalization-gap deny evidence
    assert evidence.weight_eligible is False


def test_clean_generalizer_passes() -> None:
    kp = _FakeKeypair()
    sample = RouterGeneralizationSample(
        public_score_bps=6_500,
        heldout_score_bps=6_200,
        heldout_cases=20,
        distinct_heldout_routes=8,
    )
    evidence, _ = build_signed_router_source_screen(
        keypair=kp,
        screener_hotkey=_HOTKEY,
        agent_artifact_sha256=_ARTIFACT,
        screened_image_sha256=_IMAGE,
        policy_version=1,
        sample=sample,
    )
    assert evidence.outcome is RouterSourceScreenOutcome.PASS
    assert evidence.findings == ()
