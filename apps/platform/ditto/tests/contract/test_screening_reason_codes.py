"""Guard: Platform's reason-code handling agrees with the shared registry.

``ditto_screening_protocol.reason_codes`` is the canonical screening
reason-code set and records how Platform classifies each code. Platform
derives its retry, backoff and lane tables from it; this test pins the rest of
its handling to the same classification so the worker, the retry planner and
the public pipeline cannot drift apart again (#2449, #2458, #2600):

* every registered code is known to Platform's public reason and lane maps in
  the way its classification requires;
* only an automatically retried code is ever published as "retried
  automatically", so no public text promises a retry the planner never runs;
* Platform's retry and backoff lists are exactly the registry's classes, and
  each code there is one the worker emits (or an explicitly retired code).

The worker side (every registered worker code is still emitted, and every
``retryable_infra`` exit is registered as infrastructure) is pinned in
``workers/screener/tests/test_reason_code_registry.py``.
"""

from __future__ import annotations

import pytest

from ditto.api_server import deferred_source_review, emission_eligibility
from ditto.api_server.endpoints import public, screener
from ditto.db.queries import screening, screening_infra_retry
from ditto_screening_protocol.reason_codes import (
    ADMISSION_LANE_BY_REASON_CODE,
    INFRA_AUTO_RETRY_REASON_CODES,
    PROVIDER_BACKOFF_REASON_CODES,
    SCREENING_REASON_CODES,
    SEED_PROBE_REASON_CODES,
    VERIFICATION_INCOMPLETE_UNREVIEWABLE,
    ReasonCodeClass,
    ReasonCodeProducer,
    reason_codes,
)

_AUTO_RETRY_PROMISE = "retried automatically"

# Platform maps these retired codes so historical attempts still classify and
# render. Each is a maintainer decision, not an endorsement: no worker emits it,
# yet Platform still acts on it.
# * targon-*/cloudrun-*: Targon and Cloud Run screening were retired, but the
#   codes remain in PROVIDER_BACKOFF_REASON_CODES (held on reclaim, counted
#   toward the park cap) and in the public admission-lane map.
# * source-review-retryable-infra: the model timeout's historical spelling,
#   kept in the source-review lane and the public "parked" state (#2518).
# * rust-harness-contract: superseded by container-harness-contract, kept in
#   the public reason map.
RETIRED_CODES_PLATFORM_STILL_HANDLES = frozenset(
    {
        "targon-build-unavailable",
        "targon-runtime-unavailable",
        "targon-source-review-unavailable",
        "cloudrun-build-unavailable",
        "cloudrun-runtime-unavailable",
        "source-review-retryable-infra",
        "rust-harness-contract",
    }
)


def _codes(classification: ReasonCodeClass) -> list[str]:
    return list(reason_codes(classification))


def test_platform_lists_are_the_registry_classes() -> None:
    assert (
        screening_infra_retry.INFRA_AUTO_RETRY_REASON_CODES
        == INFRA_AUTO_RETRY_REASON_CODES
    )
    assert screening.PROVIDER_BACKOFF_REASON_CODES == PROVIDER_BACKOFF_REASON_CODES
    assert (
        frozenset((*INFRA_AUTO_RETRY_REASON_CODES, *PROVIDER_BACKOFF_REASON_CODES))
        == emission_eligibility.INFRASTRUCTURE_REASON_CODES
    )
    assert dict(ADMISSION_LANE_BY_REASON_CODE) == public._ADMISSION_LANE_BY_REASON_CODE


def test_retry_and_backoff_codes_are_live_worker_codes_or_allowlisted() -> None:
    for code in (*INFRA_AUTO_RETRY_REASON_CODES, *PROVIDER_BACKOFF_REASON_CODES):
        producer = SCREENING_REASON_CODES[code].producer
        if code in RETIRED_CODES_PLATFORM_STILL_HANDLES:
            assert producer == ReasonCodeProducer.RETIRED, code
        else:
            assert producer == ReasonCodeProducer.WORKER, code


def test_retired_allowlist_matches_the_registry() -> None:
    retired = {
        code
        for code, entry in SCREENING_REASON_CODES.items()
        if entry.producer == ReasonCodeProducer.RETIRED
    }
    assert retired == RETIRED_CODES_PLATFORM_STILL_HANDLES


def test_platform_stamped_codes_are_registered_as_platform_codes() -> None:
    for code in (
        screening._ORPHANED_ATTEMPT_REASON_CODE,
        screening.LEASE_EXPIRED_REASON_CODE,
        screening.EXHAUSTED_REASON_CODE,
        VERIFICATION_INCOMPLETE_UNREVIEWABLE,
    ):
        assert SCREENING_REASON_CODES[code].producer == ReasonCodeProducer.PLATFORM
    assert {
        code
        for code, entry in SCREENING_REASON_CODES.items()
        if entry.producer == ReasonCodeProducer.PLATFORM
    } == {
        screening._ORPHANED_ATTEMPT_REASON_CODE,
        screening.LEASE_EXPIRED_REASON_CODE,
        screening.EXHAUSTED_REASON_CODE,
        # Stamped by the result endpoint as policy v13 V2.
        VERIFICATION_INCOMPLETE_UNREVIEWABLE,
    }
    assert deferred_source_review.INCONCLUSIVE_REASON_CODE in reason_codes(
        ReasonCodeClass.POLICY
    )


@pytest.mark.parametrize("code", _codes(ReasonCodeClass.FLEET_INFRA))
def test_fleet_infra_code_publishes_its_automatic_retry(code: str) -> None:
    """The verdict endpoint publishes this text for an auto-retried code."""
    assert _AUTO_RETRY_PROMISE in screener._public_screening_reason("", code)
    assert code in public._ADMISSION_LANE_BY_REASON_CODE


@pytest.mark.parametrize(
    "code",
    [
        code
        for code, entry in SCREENING_REASON_CODES.items()
        if entry.classification != ReasonCodeClass.FLEET_INFRA
    ],
)
def test_no_other_code_promises_an_automatic_retry(code: str) -> None:
    assert _AUTO_RETRY_PROMISE not in screener._public_screening_reason("", code)


@pytest.mark.parametrize("code", _codes(ReasonCodeClass.PROVIDER_BACKOFF))
def test_provider_backoff_code_names_its_lane(code: str) -> None:
    assert code in public._ADMISSION_LANE_BY_REASON_CODE


@pytest.mark.parametrize("code", _codes(ReasonCodeClass.AGENT_FAULT))
def test_agent_fault_code_has_code_specific_public_guidance(code: str) -> None:
    """A miner-owned rejection says what failed, not the generic fallback."""
    reason = screener._public_screening_reason("", code)
    assert reason != "Screening failed"
    assert code not in public._ADMISSION_LANE_BY_REASON_CODE


def test_seed_probe_public_reasons_cover_exactly_the_registered_codes() -> None:
    assert set(screener._SEED_PROBE_PUBLIC_REASONS) == set(SEED_PROBE_REASON_CODES)


def test_model_timeout_parked_codes_are_registered_source_review_codes() -> None:
    for code in public._SOURCE_REVIEW_MODEL_TIMEOUT_REASON_CODES:
        assert ADMISSION_LANE_BY_REASON_CODE[code] == "source_review"
        assert code not in INFRA_AUTO_RETRY_REASON_CODES
