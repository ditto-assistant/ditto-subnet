"""The screening reason-code registry's own invariants.

The worker and Platform each pin their side against this registry in their own
test suites (``workers/screener/tests/test_reason_code_registry.py`` and
``apps/platform/ditto/tests/test_screening_reason_code_registry.py``).
"""

from __future__ import annotations

import pytest

from ditto_screening_protocol import reason_codes as registry
from ditto_screening_protocol.reason_codes import (
    ADMISSION_LANE_BY_REASON_CODE,
    INFRA_AUTO_RETRY_REASON_CODES,
    PROVIDER_BACKOFF_REASON_CODES,
    SCREENING_REASON_CODES,
    SEED_PROBE_REASON_CODES,
    ReasonCodeClass,
    ReasonCodeProducer,
    ScreeningReasonCode,
    reason_code_class,
    reason_codes,
)

# Retry/backoff codes with no current producer. Each is an open question for
# the maintainers, not an endorsement: Platform still holds an attempt with one
# of these codes on backoff, so dropping it changes how historical rows behave.
# Targon and Cloud Run screening were retired; nothing emits their codes now.
RETIRED_RETRY_OR_BACKOFF_CODES = frozenset(
    {
        "targon-build-unavailable",
        "targon-runtime-unavailable",
        "targon-source-review-unavailable",
        "cloudrun-build-unavailable",
        "cloudrun-runtime-unavailable",
    }
)


def test_current_platform_policy_is_recorded_exactly() -> None:
    """Pins today's lists so a classification change is a visible decision.

    Widening the automatic retry is a policy change the maintainers decide
    (#1201, #2449); it must never ride along with a refactor.
    """
    assert INFRA_AUTO_RETRY_REASON_CODES == (
        "docker-build-infrastructure",
        "worker-claim-not-started",
        "l2-runtime-evidence-unavailable",
        "source-review-adjudicator-key-unavailable",
    )
    assert PROVIDER_BACKOFF_REASON_CODES == (
        "targon-build-unavailable",
        "targon-runtime-unavailable",
        "targon-source-review-unavailable",
        "cloudrun-build-unavailable",
        "cloudrun-runtime-unavailable",
        "source-review-model-timeout",
    )


def test_retry_and_backoff_lists_are_disjoint() -> None:
    # A backoff code also counts toward the park cap; an auto-retry code must
    # never feed it.
    assert not set(INFRA_AUTO_RETRY_REASON_CODES) & set(PROVIDER_BACKOFF_REASON_CODES)


@pytest.mark.parametrize(
    "code",
    [*INFRA_AUTO_RETRY_REASON_CODES, *PROVIDER_BACKOFF_REASON_CODES],
)
def test_every_retry_or_backoff_code_has_a_live_producer(code: str) -> None:
    producer = SCREENING_REASON_CODES[code].producer
    if code in RETIRED_RETRY_OR_BACKOFF_CODES:
        assert producer == ReasonCodeProducer.RETIRED
    else:
        assert producer == ReasonCodeProducer.WORKER


def test_retired_allowlist_has_no_stale_entries() -> None:
    assert RETIRED_RETRY_OR_BACKOFF_CODES.issubset(SCREENING_REASON_CODES)


def test_fleet_infra_codes_all_come_from_the_worker() -> None:
    # Platform never stamps a code it would then retry on its own say-so, and a
    # retired code must not keep an automatic retry alive.
    assert (
        reason_codes(ReasonCodeClass.FLEET_INFRA, producer=ReasonCodeProducer.WORKER)
        == INFRA_AUTO_RETRY_REASON_CODES
    )


@pytest.mark.parametrize(
    "code",
    [*INFRA_AUTO_RETRY_REASON_CODES, *PROVIDER_BACKOFF_REASON_CODES],
)
def test_every_infra_code_names_its_admission_lane(code: str) -> None:
    assert ADMISSION_LANE_BY_REASON_CODE[code] in {
        "build",
        "runtime_smoke",
        "source_review",
    }


def test_only_ditto_side_failures_name_a_lane() -> None:
    for code in ADMISSION_LANE_BY_REASON_CODE:
        assert SCREENING_REASON_CODES[code].classification in {
            ReasonCodeClass.FLEET_INFRA,
            ReasonCodeClass.PROVIDER_BACKOFF,
            ReasonCodeClass.OPERATOR_RETRY,
        }


def test_named_constants_match_their_registered_codes() -> None:
    constants = {
        name: value
        for name, value in vars(registry).items()
        if name.isupper() and not name.startswith("_") and type(value) is str
    }
    assert constants
    for name, value in constants.items():
        assert value in SCREENING_REASON_CODES, name
        assert name == value.upper().replace("-", "_"), name


def test_seed_probe_codes() -> None:
    assert SEED_PROBE_REASON_CODES == (
        "seed-readonly-write",
        "seed-memory-cap",
        "seed-exit",
        "seed-ack-invalid",
        "seed-http-error",
        "seed-oversized-response",
        "seed-unreachable",
    )


def test_unregistered_codes_stay_unclassified() -> None:
    assert reason_code_class(None) is None
    assert reason_code_class("l2-model-inconclusive") is None
    assert reason_code_class("worker-lease-orphaned") == ReasonCodeClass.OPERATOR_RETRY


@pytest.mark.parametrize(
    "bad",
    ["Docker-Build", "docker_build", "docker build", "it's", "-x", "x-", ""],
)
def test_malformed_codes_are_refused(bad: str) -> None:
    entry = ScreeningReasonCode(
        bad, ReasonCodeClass.AGENT_FAULT, ReasonCodeProducer.WORKER
    )
    with pytest.raises(ValueError, match="malformed"):
        registry._index((entry,))


def test_duplicate_codes_are_refused() -> None:
    entry = ScreeningReasonCode(
        "docker-build", ReasonCodeClass.AGENT_FAULT, ReasonCodeProducer.WORKER
    )
    with pytest.raises(ValueError, match="duplicate"):
        registry._index((entry, entry))
