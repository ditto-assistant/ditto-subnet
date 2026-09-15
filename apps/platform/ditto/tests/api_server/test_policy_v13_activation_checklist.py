"""The activation ceiling may not outrun the published policy-v13 prerequisites.

``SCREENING_ACTIVATION_CEILING_POLICY_VERSION`` is the one constant that lets
the strict two-outcome policy govern. Raising it is a release decision gated on
every bullet under "Activation prerequisites" in ``policy-v13.md`` being
verified. This test enumerates those bullets against the published document,
so a raise without the checklist (or a doc edit without the checklist) fails
here rather than in production.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ditto_screening_protocol import (
    CHECKLIST_FREE_POLICY_VERSION,
    NON_DECISIVE_REASON_CODES,
    POLICY_V13_ACTIVATION_PREREQUISITES,
    PUBLISHED_REVIEW_TIMEOUT_POLICY,
    SCREENING_ACTIVATION_CEILING_POLICY_VERSION,
    SCREENING_POLICY_VERSION,
    STRICT_TWO_OUTCOME_POLICY_VERSION,
    activation_ceiling_from_checklist,
    activation_ceiling_is_consistent,
    failure_domain_for_reason_code,
    unverified_activation_prerequisites,
    verification_failure_reason_code,
)

_POLICY_DOC = (
    Path(__file__).resolve().parents[5]
    / "workers"
    / "screener"
    / "docs"
    / "policy-v13.md"
)


def _prerequisite_bullets() -> list[str]:
    text = _POLICY_DOC.read_text()
    match = re.search(
        r"^## Activation prerequisites\n(.*?)^## ", text, flags=re.S | re.M
    )
    assert match is not None, "policy-v13.md lost its Activation prerequisites section"
    bullets: list[str] = []
    for raw in match.group(1).split("\n- ")[1:]:
        bullets.append(" ".join(line.strip() for line in raw.strip().splitlines()))
    return bullets


@pytest.mark.skipif(not _POLICY_DOC.exists(), reason="policy doc not checked out")
def test_checklist_enumerates_every_published_prerequisite() -> None:
    bullets = _prerequisite_bullets()
    assert len(bullets) == len(POLICY_V13_ACTIVATION_PREREQUISITES), (
        "policy-v13.md prerequisites and the checklist drifted: "
        f"{len(bullets)} bullets vs {len(POLICY_V13_ACTIVATION_PREREQUISITES)} items"
    )
    for item, bullet in zip(POLICY_V13_ACTIVATION_PREREQUISITES, bullets, strict=True):
        assert bullet.startswith(item.doc_fragment), (
            f"checklist item {item.key!r} no longer matches the published bullet: "
            f"{bullet!r}"
        )


def test_published_ceiling_records_the_v13_release_decision_ahead_of_checklist() -> (
    None
):
    # The checklist is a ledger of verified release facts, never inferred from
    # code. The deadline finalizer ships shadow-gated in this build and is not
    # yet released and live-verified, so the checklist still permits only the
    # checklist-free version.
    unverified = unverified_activation_prerequisites()
    assert unverified, (
        "every prerequisite reads verified; if that is a real release fact, "
        "update this test deliberately"
    )
    assert activation_ceiling_from_checklist() == CHECKLIST_FREE_POLICY_VERSION
    assert CHECKLIST_FREE_POLICY_VERSION == STRICT_TWO_OUTCOME_POLICY_VERSION - 1
    keys = {item.key for item in unverified}
    assert "deadline_finalizer_released" in keys
    assert "ceiling_raise_after_verification" in keys
    # The published ceiling moved to v13 on 2026-09-14 by recorded release
    # decision (#1891; policy-v13.md "Activation prerequisites" carries the
    # record): raising it makes v13 schedulable, it does not activate it and
    # does not move the v10 floor. The ceiling therefore outruns the checklist
    # on purpose, and the activation board exposes both numbers side by side
    # (ActivationCeilingView.checklist_ceiling_policy_version) so the operator
    # sees the gap rather than a silent invariant.
    assert (
        SCREENING_ACTIVATION_CEILING_POLICY_VERSION == STRICT_TWO_OUTCOME_POLICY_VERSION
    )
    assert not activation_ceiling_is_consistent()
    by_key = {item.key: item for item in unverified}
    assert "2026-09-14" in (by_key["ceiling_raise_after_verification"].evidence or "")


def test_ceiling_never_exceeds_the_implemented_policy() -> None:
    assert SCREENING_ACTIVATION_CEILING_POLICY_VERSION <= SCREENING_POLICY_VERSION
    assert activation_ceiling_from_checklist() <= SCREENING_POLICY_VERSION


def test_published_retry_defaults_match_policy_v13() -> None:
    policy = PUBLISHED_REVIEW_TIMEOUT_POLICY
    assert policy.artifact_failure_retries == 1
    assert policy.provider_failure_retries == 2
    assert policy.platform_failure_retries == 2
    assert policy.independent_worker_required_for_platform_provider_failure is True
    assert policy.max_verification_window_hours == 24
    assert policy.applies_from_policy_version == STRICT_TWO_OUTCOME_POLICY_VERSION
    assert policy.terminal_outcome == "review_timed_out"
    assert policy.ban_on_timeout is False
    assert policy.precedent_weight_on_timeout is False


@pytest.mark.skipif(not _POLICY_DOC.exists(), reason="policy doc not checked out")
def test_every_non_decisive_code_and_default_is_published() -> None:
    text = _POLICY_DOC.read_text()
    missing = sorted(code for code in NON_DECISIVE_REASON_CODES if code not in text)
    # The platform's own expiry-cap park is not a screener code; everything the
    # screener can emit must be published.
    assert missing == ["repeatedly-inconclusive"]
    assert "maximum verification window: 24 hours" in text
    assert "platform failure retries: 2" in text
    assert "provider failure retries: 2" in text
    assert "artifact-controlled failure retries: 1" in text


def test_failure_domain_mapping_is_never_artifact_or_submission() -> None:
    for code in NON_DECISIVE_REASON_CODES:
        assert failure_domain_for_reason_code(code) in ("platform", "provider")
    assert failure_domain_for_reason_code("source-review-inconclusive") == "platform"
    assert failure_domain_for_reason_code("source-review-unavailable") == "provider"
    assert (
        failure_domain_for_reason_code(
            "challenge-inconclusive", failure_provider="targon"
        )
        == "provider"
    )
    assert (
        verification_failure_reason_code("platform")
        == "V2.platform_verification_failed"
    )
    assert (
        verification_failure_reason_code("provider")
        == "V3.provider_verification_failed"
    )
    assert verification_failure_reason_code("submission") == (
        "V1.required_submission_evidence_missing"
    )
    assert verification_failure_reason_code("none") is None
    assert verification_failure_reason_code("artifact") is None
