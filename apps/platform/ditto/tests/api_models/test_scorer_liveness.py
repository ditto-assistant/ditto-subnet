"""The scorer liveness probe carried on heartbeat protocol v15.

Two outages produced this field, and both are pinned here.

*A dead sidecar that read as merely old.* A validator's dittobench-api answered
``404`` on ``/v1/capabilities``. The validator did the correct fail-closed thing
and reported ``legacy_v2`` with every identity field null -- which is exactly
what a genuine, healthy, pre-capabilities scorer reports. Nothing on the wire
separated "this scorer is gone" from "this scorer is old", so the fleet view
showed ``warning`` beside ``accepting`` and the validator kept leasing work it
could not do.

*A readable reply that was quietly half-rejected.* A scorer added one
descriptive key to its capability document. The validator dropped bench v7 and
went on reporting ``fresh_verified`` and ``healthy``, because every field it did
read still matched. Green everywhere, no v7 capability.

The probe is the evidence behind the conclusion: what the request did, what came
back, and when the scorer last actually served.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ditto.api_models.validator_capabilities import (
    ScorerBenchmarkCapability,
    ScorerLivenessProbe,
    ValidatorCapabilities,
    ValidatorStackIdentity,
    validator_identity_signing_token,
)

_OBSERVED = 1_784_020_800


def _verified(**overrides: object) -> ScorerBenchmarkCapability:
    values: dict[str, object] = {
        "status": "fresh_verified",
        "supported_bench_versions": (2, 3),
        "observed_at": _OBSERVED,
        "software_version": "0.29.4",
        "source_revision": "a" * 40,
        **overrides,
    }
    return ScorerBenchmarkCapability(**values)  # type: ignore[arg-type]


def test_a_dead_sidecar_is_distinguishable_from_a_genuinely_old_scorer() -> None:
    """The TAO.com outage: both report ``legacy_v2`` and nulls; only one is fine."""
    dead = ScorerBenchmarkCapability(
        status="legacy_v2",
        supported_bench_versions=(2,),
        probe=ScorerLivenessProbe(
            outcome="http_error",
            observed_at=_OBSERVED,
            http_status=404,
            consecutive_failures=97,
        ),
    )
    genuinely_old = ScorerBenchmarkCapability(
        status="legacy_v2",
        supported_bench_versions=(2,),
        probe=ScorerLivenessProbe(
            outcome="http_error",
            observed_at=_OBSERVED,
            http_status=404,
            last_served_at=None,
            consecutive_failures=1,
        ),
    )

    # The conclusion is identical -- which is the whole problem.
    assert dead.status == genuinely_old.status
    assert dead.supported_bench_versions == genuinely_old.supported_bench_versions
    assert (dead.observed_at, dead.software_version, dead.source_revision) == (
        None,
        None,
        None,
    )
    # The evidence is not. A route that has answered 404 ninety-seven times
    # running is a sidecar that never mounted it, not a scorer from 2025.
    assert dead.probe is not None and dead.probe.consecutive_failures == 97
    assert dead.probe != genuinely_old.probe


def test_an_unknown_field_in_the_scorer_reply_must_not_read_as_healthy() -> None:
    """The v7 capability-parse bug, at the layer that reports it.

    Dropping an unreadable calibration is correct. Dropping it while still
    reporting a fully served probe is what hid the outage for hours, so the
    model refuses to represent that combination: a capability the validator
    narrowed is ``served_degraded`` and names why.
    """
    narrowed = _verified(
        supported_bench_versions=(2, 3, 4, 5, 6),
        probe=ScorerLivenessProbe(
            outcome="served_degraded",
            observed_at=_OBSERVED,
            http_status=200,
            reason="calibration_unreadable",
            last_served_at=_OBSERVED,
            consecutive_failures=1,
        ),
    )
    assert narrowed.status == "fresh_verified"
    assert narrowed.probe is not None
    assert narrowed.probe.reason == "calibration_unreadable"

    # A probe cannot claim it read the whole document while the validator is
    # advertising less than the scorer offered -- "served" means served.
    with pytest.raises(ValidationError, match="no outstanding probe failures"):
        ScorerLivenessProbe(
            outcome="served",
            observed_at=_OBSERVED,
            http_status=200,
            last_served_at=_OBSERVED,
            consecutive_failures=1,
        )


def test_a_green_scorer_status_requires_a_probe_that_actually_served() -> None:
    with pytest.raises(ValidationError, match="must have served its probe"):
        _verified(
            probe=ScorerLivenessProbe(
                outcome="connect_error", observed_at=_OBSERVED, consecutive_failures=4
            )
        )
    with pytest.raises(ValidationError, match="cannot have served its probe"):
        ScorerBenchmarkCapability(
            status="unreachable",
            supported_bench_versions=(2,),
            probe=ScorerLivenessProbe(
                outcome="served",
                observed_at=_OBSERVED,
                http_status=200,
                last_served_at=_OBSERVED,
            ),
        )
    with pytest.raises(ValidationError, match="must be named by the probe"):
        ScorerBenchmarkCapability(
            status="identity_mismatch",
            supported_bench_versions=(2,),
            probe=ScorerLivenessProbe(
                outcome="served_degraded",
                observed_at=_OBSERVED,
                http_status=200,
                reason="malformed_capabilities",
                last_served_at=_OBSERVED,
                consecutive_failures=1,
            ),
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        pytest.param(
            {"outcome": "connect_error", "http_status": 502},
            "exactly when it got an answer",
            id="unreachable-cannot-report-a-status",
        ),
        pytest.param(
            {"outcome": "http_error"},
            "exactly when it got an answer",
            id="http-error-must-report-a-status",
        ),
        pytest.param(
            {"outcome": "http_error", "http_status": 200},
            "not an HTTP error",
            id="200-is-not-an-error",
        ),
        pytest.param(
            {"outcome": "unreadable", "http_status": 503, "reason": "invalid_json"},
            "must have received 200",
            id="a-read-body-implies-200",
        ),
        pytest.param(
            {"outcome": "unreadable", "http_status": 200},
            "must name its reason",
            id="unreadable-must-say-why",
        ),
        pytest.param(
            {
                "outcome": "timeout",
                "reason": "invalid_json",
                "consecutive_failures": 1,
            },
            "must name its reason",
            id="a-timeout-read-nothing-to-have-a-reason-about",
        ),
        pytest.param(
            {"outcome": "served", "http_status": 200, "last_served_at": _OBSERVED - 60},
            "last served at its own observation",
            id="serving-now-means-served-now",
        ),
        pytest.param(
            {"outcome": "timeout", "last_served_at": _OBSERVED + 1},
            "cannot have served after it was observed",
            id="no-service-from-the-future",
        ),
    ],
)
def test_probe_evidence_cannot_contradict_itself(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ScorerLivenessProbe(observed_at=_OBSERVED, **kwargs)  # type: ignore[arg-type]


def test_an_absent_probe_reproduces_the_legacy_signing_bytes_exactly() -> None:
    """The compatibility guarantee, checked against the frozen v7 vector.

    A validator that predates v15 signs what it always signed. The heartbeat is
    signed, so an additive field must not change the canonical token produced
    by an older reporter.
    """
    fixture = json.loads(
        (
            Path(__file__).parents[1] / "contract" / "validator_heartbeat_v7.json"
        ).read_text()
    )
    capabilities = ValidatorCapabilities.model_validate(
        fixture["request"]["capabilities"]
    )
    stack = ValidatorStackIdentity.model_validate(fixture["request"]["stack"])

    assert capabilities.scorer_benchmarks is None
    assert (
        validator_identity_signing_token(capabilities, stack)
        in fixture["expected_message_utf8"]
    )

    # And one level in: a scorer capability with no probe serializes exactly as
    # it did before the field existed.
    without_probe = _verified()
    assert "probe" not in without_probe.model_dump(mode="json", exclude_none=True)
