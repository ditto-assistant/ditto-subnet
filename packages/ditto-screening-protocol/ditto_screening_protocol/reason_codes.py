"""Canonical screening reason codes and how Platform classifies each one.

A screening attempt carries one bounded ``reason_code``. The screener worker
produces most of them, Platform stamps a few itself, and Platform decides from
the code whether a failed attempt retries automatically, backs off, or parks
for an operator. Those lists used to be hand-copied between the worker,
Platform's retry planner, its claim backoff and its public pipeline, and they
drifted: Platform listed a backoff code no worker emitted (#2458, #2518) and
docstrings promised retries the code did not do (#2449). Both sides now import
the codes and their classification from here.

This module records the classification Platform applies today; it does not
decide policy. Moving a code to ``FLEET_INFRA`` widens Platform's automatic
retry and is a deliberate policy change (#1201, #2449): every producer of the
code must be reachable only from fleet, transport or lock state, never from
the submitted artifact, a budget or a lease. It also means updating the partial
index ``screening_attempts_infra_failed_idx`` in Platform (``models.py``, a new
migration, and ``_infra_failure_filters``) in the same change.

A code absent from the registry is unclassified. Platform treats it as it
always has (an operator retry, no admission lane, and a detail-derived public
reason), so this registry adds no default.

The classification describes the code's failed (``retryable_infra``) form.
``l2-runtime-evidence-unavailable`` also reaches Platform as an INCONCLUSIVE
verdict with an ``expired`` attempt, which no retry list matches.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal


class ReasonCodeClass(StrEnum):
    """How Platform treats an attempt that failed with a code."""

    FLEET_INFRA = "fleet_infra"
    """Fleet-owned and independent of the artifact. Platform's bounded infra
    planner retries it automatically within its backoff, breaker, age and
    streak caps, and it never counts toward the inconclusive park cap."""

    PROVIDER_BACKOFF = "provider_backoff"
    """A provider or reviewer outage. Held on reclaim for a short backoff and,
    with peer-pass evidence, counted toward the inconclusive park cap. Never
    retried automatically."""

    OPERATOR_RETRY = "operator_retry"
    """Infrastructure-shaped, but a submission, a budget or a lease can reach
    it, so the attempt parks until an operator authorizes one exact retry."""

    AGENT_FAULT = "agent_fault"
    """The submitted artifact failed a deterministic contract; the miner owns
    the failure and Platform publishes code-specific guidance."""

    POLICY = "policy"
    """A review or Platform policy outcome without a fault finding: a hold, or
    the terminal V2 reject (``violation_proven: false``)."""


class ReasonCodeProducer(StrEnum):
    """Which component writes the code onto a screening attempt."""

    WORKER = "worker"
    PLATFORM = "platform"
    RETIRED = "retired"
    """No current producer. Kept so historical attempts still classify and
    render; removing one changes how those rows are treated."""


AdmissionLane = Literal["build", "runtime_smoke", "source_review"]
"""The Ditto-side admission lane a failure stopped in (Platform's public
``PublicAdmissionLane``)."""


@dataclass(frozen=True, slots=True)
class ScreeningReasonCode:
    """One registered reason code."""

    code: str
    classification: ReasonCodeClass
    producer: ReasonCodeProducer
    lane: AdmissionLane | None = None
    """Set only when the public pipeline can vouch for the lane that failed."""


# Codes imported by name in the worker or Platform.
DOCKER_BUILD_INFRASTRUCTURE: Final = "docker-build-infrastructure"
WORKER_CLAIM_NOT_STARTED: Final = "worker-claim-not-started"
L2_RUNTIME_EVIDENCE_UNAVAILABLE: Final = "l2-runtime-evidence-unavailable"
SOURCE_REVIEW_ADJUDICATOR_KEY_UNAVAILABLE: Final = (
    "source-review-adjudicator-key-unavailable"
)
TARGON_BUILD_UNAVAILABLE: Final = "targon-build-unavailable"
TARGON_RUNTIME_UNAVAILABLE: Final = "targon-runtime-unavailable"
TARGON_SOURCE_REVIEW_UNAVAILABLE: Final = "targon-source-review-unavailable"
CLOUDRUN_BUILD_UNAVAILABLE: Final = "cloudrun-build-unavailable"
CLOUDRUN_RUNTIME_UNAVAILABLE: Final = "cloudrun-runtime-unavailable"
SOURCE_REVIEW_MODEL_TIMEOUT: Final = "source-review-model-timeout"
SOURCE_REVIEW_PROVIDER_CREDITS_EXHAUSTED: Final = (
    "source-review-provider-credits-exhausted"
)
SOURCE_REVIEW_RETRYABLE_INFRA: Final = "source-review-retryable-infra"
WORKER_LEASE_ORPHANED: Final = "worker-lease-orphaned"
SCREENING_LEASE_EXPIRED: Final = "screening-lease-expired"
SOURCE_REVIEW_INCONCLUSIVE: Final = "source-review-inconclusive"
REPEATEDLY_INCONCLUSIVE: Final = "repeatedly-inconclusive"
SOURCE_REVIEW_CONFIRMED_VIOLATION: Final = "source-review-confirmed-violation"
"""A policy v13 invariant breach whose causal proof is complete and which the
independent L3 violation adjudicator confirmed after it could have refuted it.
The worker transports it as a hold; Platform records the terminal reject."""
VERIFICATION_INCOMPLETE_UNREVIEWABLE: Final = "verification-incomplete-unreviewable"
"""Policy v13 ``V2.platform_verification_failed``: Platform's terminal reject
after ``V2_COMPLETE_STATIC_REVIEWS`` complete source reviews of the exact
artifact all ended ``l2-model-inconclusive`` (``insufficient_static_evidence``).
Not misconduct: the miner may resubmit, and nothing is banned."""
V2_COMPLETE_STATIC_REVIEWS: Final = 2
"""Published retry count behind ``VERIFICATION_INCOMPLETE_UNREVIEWABLE``
(docs/policy-v13.md, V2). Budget, time, provider and infrastructure stops end
with other codes and never count."""

_F = ReasonCodeClass.FLEET_INFRA
_P = ReasonCodeClass.PROVIDER_BACKOFF
_O = ReasonCodeClass.OPERATOR_RETRY
_A = ReasonCodeClass.AGENT_FAULT
_POLICY = ReasonCodeClass.POLICY
_WORKER = ReasonCodeProducer.WORKER
_PLATFORM = ReasonCodeProducer.PLATFORM
_RETIRED = ReasonCodeProducer.RETIRED

# Order is significant within a class: derived tuples keep it.
_REGISTRY: Final[tuple[ScreeningReasonCode, ...]] = (
    # --- FLEET_INFRA: Platform's automatic, bounded retry.
    # Docker daemon, BuildKit, or the build host failed (or the build lease
    # expired); the archive was never judged.
    ScreeningReasonCode(DOCKER_BUILD_INFRASTRUCTURE, _F, _WORKER, "build"),
    # Settled between claim and screen, before the artifact was fetched (#2446).
    ScreeningReasonCode(WORKER_CLAIM_NOT_STARTED, _F, _WORKER, "build"),
    # No signed scorer-cohort lease was attached at claim (#2444).
    ScreeningReasonCode(L2_RUNTIME_EVIDENCE_UNAVAILABLE, _F, _WORKER, "source_review"),
    # The node's source-review court key file was unusable; it is read before
    # the archive is opened (#2449).
    ScreeningReasonCode(
        SOURCE_REVIEW_ADJUDICATOR_KEY_UNAVAILABLE, _F, _WORKER, "source_review"
    ),
    # --- PROVIDER_BACKOFF: held on reclaim, counted with peer evidence.
    ScreeningReasonCode(TARGON_BUILD_UNAVAILABLE, _P, _RETIRED, "build"),
    ScreeningReasonCode(TARGON_RUNTIME_UNAVAILABLE, _P, _RETIRED, "runtime_smoke"),
    ScreeningReasonCode(
        TARGON_SOURCE_REVIEW_UNAVAILABLE, _P, _RETIRED, "source_review"
    ),
    ScreeningReasonCode(CLOUDRUN_BUILD_UNAVAILABLE, _P, _RETIRED, "build"),
    ScreeningReasonCode(CLOUDRUN_RUNTIME_UNAVAILABLE, _P, _RETIRED, "runtime_smoke"),
    # An L1 model turn timed out while the lease still had time (lease expiry
    # reports source-review-lease-budget-exhausted instead); immediate reclaim
    # would hot-loop against the same broken court (#2458).
    ScreeningReasonCode(SOURCE_REVIEW_MODEL_TIMEOUT, _P, _WORKER, "source_review"),
    # --- OPERATOR_RETRY: parks for an exact operator retry.
    # The model timeout's historical spelling, never emitted by a worker; kept
    # so older rows still render in the source-review lane (#2458, #2518).
    ScreeningReasonCode(SOURCE_REVIEW_RETRYABLE_INFRA, _O, _RETIRED, "source_review"),
    # Platform's orphan sweep: no live worker still reports the lease.
    ScreeningReasonCode(WORKER_LEASE_ORPHANED, _O, _PLATFORM),
    # Platform's expiry sweep; also counts toward the inconclusive park cap.
    ScreeningReasonCode(SCREENING_LEASE_EXPIRED, _O, _PLATFORM),
    ScreeningReasonCode("worker-platform-request-failed", _O, _WORKER),
    ScreeningReasonCode("worker-verdict-rejected", _O, _WORKER),
    ScreeningReasonCode("worker-verdict-auth-failed", _O, _WORKER),
    ScreeningReasonCode("worker-result-processing-failed", _O, _WORKER),
    ScreeningReasonCode("unexpected-infrastructure", _O, _WORKER),
    ScreeningReasonCode("lease-budget-exhausted", _O, _WORKER),
    ScreeningReasonCode("source-review-unavailable", _O, _WORKER),
    ScreeningReasonCode("l2-cache-lock-timeout", _O, _WORKER),
    ScreeningReasonCode("l2-late-result", _O, _WORKER),
    # The review gateway answered HTTP 402: the account paying for review
    # inference is out of credits or its spend is not authorized. Fleet
    # billing, never the artifact, so it must never count toward a park cap;
    # the worker stops claiming until a probe succeeds, and the operator
    # retries the parked attempts once the account is funded.
    ScreeningReasonCode(SOURCE_REVIEW_PROVIDER_CREDITS_EXHAUSTED, _O, _WORKER),
    ScreeningReasonCode("static-preflight-audit-failed", _O, _WORKER),
    ScreeningReasonCode("executor-isolation-unavailable", _O, _WORKER),
    ScreeningReasonCode("replay-image-identity-mismatch", _O, _WORKER),
    ScreeningReasonCode("replay-image-load-failed", _O, _WORKER),
    ScreeningReasonCode("screened-image-export-failed", _O, _WORKER),
    ScreeningReasonCode("image-upload-failed", _O, _WORKER),
    # --- AGENT_FAULT: deterministic, miner-owned rejections.
    ScreeningReasonCode("docker-build", _A, _WORKER),
    ScreeningReasonCode("docker-build-timeout", _A, _WORKER),
    ScreeningReasonCode("container-harness-contract", _A, _WORKER),
    # The Rust-only harness contract was replaced by the container contract.
    ScreeningReasonCode("rust-harness-contract", _A, _RETIRED),
    ScreeningReasonCode("exact-cross-miner-duplicate", _A, _WORKER),
    # An L3-confirmed policy v13 invariant breach (Platform rejects it).
    ScreeningReasonCode(SOURCE_REVIEW_CONFIRMED_VIOLATION, _A, _WORKER),
    # Seeding-probe rejections: the image could not serve POST /seed.
    ScreeningReasonCode("seed-readonly-write", _A, _WORKER),
    ScreeningReasonCode("seed-memory-cap", _A, _WORKER),
    ScreeningReasonCode("seed-exit", _A, _WORKER),
    ScreeningReasonCode("seed-ack-invalid", _A, _WORKER),
    ScreeningReasonCode("seed-http-error", _A, _WORKER),
    ScreeningReasonCode("seed-oversized-response", _A, _WORKER),
    ScreeningReasonCode("seed-unreachable", _A, _WORKER),
    # --- POLICY: held without a fault finding.
    ScreeningReasonCode(SOURCE_REVIEW_INCONCLUSIVE, _POLICY, _WORKER),
    # Platform's exhaustion hold after repeated inconclusive attempts.
    ScreeningReasonCode(REPEATEDLY_INCONCLUSIVE, _POLICY, _PLATFORM),
    # Platform's V2 reject once complete static reviews stay inconclusive.
    ScreeningReasonCode(VERIFICATION_INCOMPLETE_UNREVIEWABLE, _POLICY, _PLATFORM),
)

# Lowercase kebab case: safe to inline as a SQL literal (the infra planner's
# partial-index predicate does) and bounded like every wire reason code.
_CODE_SHAPE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _index(
    entries: tuple[ScreeningReasonCode, ...],
) -> Mapping[str, ScreeningReasonCode]:
    by_code: dict[str, ScreeningReasonCode] = {}
    for entry in entries:
        if _CODE_SHAPE.fullmatch(entry.code) is None:
            raise ValueError(f"malformed screening reason code {entry.code!r}")
        if entry.code in by_code:
            raise ValueError(f"duplicate screening reason code {entry.code!r}")
        by_code[entry.code] = entry
    return MappingProxyType(by_code)


SCREENING_REASON_CODES: Final[Mapping[str, ScreeningReasonCode]] = _index(_REGISTRY)
"""Every registered code, in registry order."""


def reason_codes(
    classification: ReasonCodeClass,
    *,
    producer: ReasonCodeProducer | None = None,
) -> tuple[str, ...]:
    """Registered codes in ``classification`` (optionally one producer's)."""
    return tuple(
        entry.code
        for entry in SCREENING_REASON_CODES.values()
        if entry.classification == classification
        and (producer is None or entry.producer == producer)
    )


def reason_code_class(code: str | None) -> ReasonCodeClass | None:
    """The registered classification of ``code``; ``None`` when unregistered."""
    entry = SCREENING_REASON_CODES.get(code) if code is not None else None
    return entry.classification if entry is not None else None


INFRA_AUTO_RETRY_REASON_CODES: Final[tuple[str, ...]] = reason_codes(
    ReasonCodeClass.FLEET_INFRA
)
"""Platform's automatic infra-retry allowlist (``screening_infra_retry``)."""

PROVIDER_BACKOFF_REASON_CODES: Final[tuple[str, ...]] = reason_codes(
    ReasonCodeClass.PROVIDER_BACKOFF
)
"""Platform's provider backoff list (``screening.py``)."""

ADMISSION_LANE_BY_REASON_CODE: Final[Mapping[str, AdmissionLane]] = MappingProxyType(
    {
        entry.code: entry.lane
        for entry in SCREENING_REASON_CODES.values()
        if entry.lane is not None
    }
)
"""The public pipeline's lane for each Ditto-side admission failure."""

SEED_PROBE_REASON_CODES: Final[tuple[str, ...]] = tuple(
    code
    for code in reason_codes(ReasonCodeClass.AGENT_FAULT)
    if code.startswith("seed-")
)
"""Seeding-probe rejections, each with its own public guidance in Platform."""
