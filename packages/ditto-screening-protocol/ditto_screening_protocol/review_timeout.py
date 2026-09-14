"""Published v13 retry/deadline policy and the activation-ceiling checklist.

Policy v13 (``workers/screener/docs/policy-v13.md``) resolves every review to
``CLEAR`` or ``REJECT`` and requires every non-decisive processing state to
terminate by a published deadline. Platform implements that deadline as a
distinct, no-fault terminal ``review_timed_out`` decision: never a plain
``REJECT``, never a ban, never precedent, always a public no-fault reason with
the failure domain and retry evidence the policy's decision record demands,
and always an automatic no-fault retry so the submission re-enters priority
screening when capacity recovers.

Everything an operator or miner can be told about that treatment lives here as
shared constants so the Platform job, the admin API, the Backroom read tool and
the tests all cite one source. Versions below ``STRICT_TWO_OUTCOME_POLICY_VERSION``
keep their signed compatibility behaviour and are never finalized.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from ditto_screening_protocol.models import (
    SCREENING_ACTIVATION_CEILING_POLICY_VERSION,
    SCREENING_POLICY_VERSION,
    STRICT_TWO_OUTCOME_POLICY_VERSION,
)

# ---------------------------------------------------------------------------
# Terminal decision outcomes and the no-fault timeout identity.
# ---------------------------------------------------------------------------

ScreeningDecisionOutcome = Literal["clear", "reject", "review_timed_out"]
FailureDomain = Literal["artifact", "submission", "platform", "provider", "none"]

# The Platform finalizer's posture. ``shadow`` selects and dry-runs every
# would-be decision (logged, rolled back) without writing; ``enforce`` performs
# the mutation; ``off`` never queries. New automated decision paths ship in
# shadow so an operator sees a real dry run before the first live timeout.
ReviewTimeoutFinalizerMode = Literal["off", "shadow", "enforce"]
REVIEW_TIMEOUT_FINALIZER_MODE_ENV = "DITTO_REVIEW_TIMEOUT_FINALIZER_MODE"
DEFAULT_REVIEW_TIMEOUT_FINALIZER_MODE: ReviewTimeoutFinalizerMode = "shadow"

REVIEW_TIMED_OUT_OUTCOME: ScreeningDecisionOutcome = "review_timed_out"
# Reason code stamped on the agent/attempt (hyphenated, matches the DB regex).
REVIEW_TIMED_OUT_REASON_CODE = "review-timed-out"
REVIEW_TIMED_OUT_PUBLIC_REASON = (
    "Screening review did not complete within the published verification "
    "window; this is a no-fault timeout, not a finding against the submission. "
    "The submission is queued for priority rescreen when review capacity "
    "recovers."
)
REVIEW_TIMEOUT_FINALIZER_ACTOR = "platform:review-timeout-finalizer"

# Published V-code reason codes from policy-v13.md "Required reason codes".
V1_REQUIRED_SUBMISSION_EVIDENCE_MISSING = "V1.required_submission_evidence_missing"
V2_PLATFORM_VERIFICATION_FAILED = "V2.platform_verification_failed"
V3_PROVIDER_VERIFICATION_FAILED = "V3.provider_verification_failed"

# Screener/platform codes that reach no decision (policy-v13.md "Non-decisive
# results"). Each resolves through the retry and deadline procedure; none may
# ever produce CLEAR or read as a finding.
NON_DECISIVE_REASON_CODES: frozenset[str] = frozenset(
    {
        "source-review-inconclusive",
        "source-review-invalid-risk",
        "source-review-inconsistent-verdict",
        "adjudicated-source-review-escalate",
        "behavioral-oracle-inconclusive",
        "challenge-inconclusive",
        "source-review-unavailable",
        # Platform-raised park after the expiry cap; the screen never concluded.
        "repeatedly-inconclusive",
    }
)

# Codes whose non-decision was the upstream model/provider's, not the platform's.
_PROVIDER_DOMAIN_REASON_CODES: frozenset[str] = frozenset(
    {
        "source-review-invalid-risk",
        "source-review-inconsistent-verdict",
        "source-review-unavailable",
    }
)

_FAILURE_DOMAIN_REASON_CODE: dict[str, str] = {
    "submission": V1_REQUIRED_SUBMISSION_EVIDENCE_MISSING,
    "platform": V2_PLATFORM_VERIFICATION_FAILED,
    "provider": V3_PROVIDER_VERIFICATION_FAILED,
}


def failure_domain_for_reason_code(
    reason_code: str | None, *, failure_provider: str | None = None
) -> FailureDomain:
    """Classify one non-decisive code into the policy's failure domain.

    A recorded upstream provider on the attempt is authoritative (V3). Codes
    that name an unusable model verdict are provider failures; every other
    non-decision is the screener/platform's inability to complete the check
    (V2). ``artifact`` is reserved for proven violations and is never inferred
    here; ``submission`` is never inferred either because a bounded review that
    ran out of budget says nothing about the artifact's own evidence.
    """
    if failure_provider:
        return "provider"
    if reason_code in _PROVIDER_DOMAIN_REASON_CODES:
        return "provider"
    return "platform"


def verification_failure_reason_code(failure_domain: FailureDomain) -> str | None:
    """Map a verification-failure domain onto its published V-code."""
    return _FAILURE_DOMAIN_REASON_CODE.get(failure_domain)


# ---------------------------------------------------------------------------
# Published retry, deadline and capacity thresholds.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewTimeoutPolicy:
    """The published retry and deadline procedure (policy-v13.md defaults).

    ``review_timed_out`` is the Platform's terminal treatment of a processing
    state that outlives ``max_verification_window``; it is not one of the
    policy's two review outcomes, carries no precedent weight, and grants a
    no-fault retry so the deadline never converts a screener outage into a
    penalty for an honest miner.
    """

    artifact_failure_retries: int = 1
    provider_failure_retries: int = 2
    platform_failure_retries: int = 2
    independent_worker_required_for_platform_provider_failure: bool = True
    max_verification_window: timedelta = timedelta(hours=24)
    # The oldest policy version the finalizer acts on; older attempts keep
    # their signed compatibility behaviour (holds stay operator-owned).
    applies_from_policy_version: int = STRICT_TWO_OUTCOME_POLICY_VERSION
    terminal_outcome: ScreeningDecisionOutcome = REVIEW_TIMED_OUT_OUTCOME
    ban_on_timeout: bool = False
    precedent_weight_on_timeout: bool = False
    automatic_priority_rescreen_on_recovery: bool = True
    no_fault_retry_grant_on_timeout: bool = True

    @property
    def max_verification_window_hours(self) -> int:
        return int(self.max_verification_window.total_seconds() // 3600)

    def automatic_retry_budget(self, failure_domain: FailureDomain) -> int:
        """Published automatic retries for one failure domain.

        ``artifact`` and ``submission`` failures are the miner's to fix and get
        the artifact budget; ``none`` is not a failure and grants nothing.
        """
        if failure_domain == "provider":
            return self.provider_failure_retries
        if failure_domain == "platform":
            return self.platform_failure_retries
        if failure_domain in ("artifact", "submission"):
            return self.artifact_failure_retries
        return 0

    @staticmethod
    def automatic_retries_used(retry_count: int) -> int:
        """Retries already spent: every strict-policy attempt after the first."""
        return max(int(retry_count) - 1, 0)

    def independent_worker_required(self, failure_domain: FailureDomain) -> bool:
        return (
            self.independent_worker_required_for_platform_provider_failure
            and failure_domain in ("platform", "provider")
        )

    def automatic_retry_permitted(
        self,
        failure_domain: FailureDomain,
        *,
        retry_count: int,
        independent_workers: int,
    ) -> bool:
        """Whether the finalizer may mint another no-fault retry grant.

        The budget counts every strict-policy attempt after the first as one
        automatic retry, whichever worker ran it: a same-worker repeat still
        consumed screener capacity, so it cannot be free or a single-worker
        fleet would cycle a permanently inconclusive submission forever. When
        the policy requires an independent worker for platform/provider
        failures, the recorded ``independent_workers`` is published with the
        decision so an operator can see whether that requirement was ever
        met; past the cap the timeout is still recorded (no ban, no
        precedent) but the retry becomes the operator's call.
        """
        del independent_workers  # evidence for the record, not a gate
        return self.automatic_retries_used(retry_count) < self.automatic_retry_budget(
            failure_domain
        )


@dataclass(frozen=True)
class ReviewCapacityThresholds:
    """Published review-capacity, completion, latency and backlog thresholds.

    Activation of the strict two-outcome policy is gated on the fleet meeting
    these; the finalizer publishes them alongside every decision so an
    operator reading a timeout sees the bar the fleet was held to.
    """

    min_healthy_source_review_workers: int = 1
    min_completion_rate: float = 0.95
    max_fail_open_rate: float = 0.05
    max_p95_review_latency: timedelta = timedelta(hours=12)
    max_backlog_multiplier: int = 3

    @property
    def max_p95_review_latency_hours(self) -> int:
        return int(self.max_p95_review_latency.total_seconds() // 3600)


PUBLISHED_REVIEW_TIMEOUT_POLICY = ReviewTimeoutPolicy()
PUBLISHED_REVIEW_CAPACITY_THRESHOLDS = ReviewCapacityThresholds()


# ---------------------------------------------------------------------------
# Activation-ceiling checklist (policy-v13.md "Activation prerequisites").
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivationPrerequisite:
    """One published prerequisite that must be verified before a ceiling raise.

    ``doc_fragment`` is a verbatim fragment of the policy bullet so a test can
    prove the checklist still enumerates the published text. ``verified`` is a
    recorded operator fact with its evidence, never inferred from code.
    """

    key: str
    summary: str
    doc_fragment: str
    verified: bool = False
    evidence: str | None = None


# Every bullet under "## Activation prerequisites" in policy-v13.md, in order.
POLICY_V13_ACTIVATION_PREREQUISITES: tuple[ActivationPrerequisite, ...] = (
    ActivationPrerequisite(
        key="opaque_role_verification_spec",
        summary=(
            "Opaque-component role-verification spec and equivalent-evidence "
            "routes published"
        ),
        doc_fragment=("Publish the opaque-component role-verification specification"),
    ),
    ActivationPrerequisite(
        key="mandatory_routes_operational",
        summary=(
            "Every mandatory verification route for the activated scope is operational"
        ),
        doc_fragment=(
            "Make every mandatory verification route for the activated scope"
        ),
    ),
    ActivationPrerequisite(
        key="capacity_thresholds_met",
        summary=(
            "Review-capacity, completion, latency and backlog thresholds "
            "published and met"
        ),
        doc_fragment=(
            "Publish and meet review-capacity, completion, latency, and backlog"
        ),
        evidence=(
            "thresholds published: ditto_screening_protocol.review_timeout."
            "PUBLISHED_REVIEW_CAPACITY_THRESHOLDS; fleet measurement pending"
        ),
    ),
    ActivationPrerequisite(
        key="retry_deadlines_published",
        summary=("Retry deadlines and platform/provider failure treatment published"),
        doc_fragment=(
            "Publish retry deadlines and platform/provider failure treatment"
        ),
        evidence=(
            "ditto_screening_protocol.review_timeout."
            "PUBLISHED_REVIEW_TIMEOUT_POLICY; verified on release"
        ),
    ),
    ActivationPrerequisite(
        key="lifecycle_rules_published",
        summary=(
            "Transition, expiry, suspension, revocation, weight-cutoff, "
            "convergence and rollback rules published"
        ),
        doc_fragment=(
            "Publish transition, approval-expiry, suspension, revocation, weight-cutoff"
        ),
    ),
    ActivationPrerequisite(
        key="exact_artifact_approval_gate",
        summary=(
            "Exact-artifact approval gate verified through ranking and weight "
            "generation"
        ),
        doc_fragment=(
            "Verify the exact-artifact approval gate through ranking and weight"
        ),
    ),
    ActivationPrerequisite(
        key="staged_activation_names_scope",
        summary=(
            "A staged activation names the exact rules/tests/artifact classes it covers"
        ),
        doc_fragment=(
            "A staged activation names the exact rules/tests/artifact classes"
        ),
    ),
    ActivationPrerequisite(
        key="no_scheduled_safeguards",
        summary=(
            "Scheduled/unavailable verification is not counted as an "
            "implemented safeguard"
        ),
        doc_fragment=(
            "Scheduled/unavailable verification is not an implemented safeguard"
        ),
    ),
    ActivationPrerequisite(
        key="fleet_protocol_guard_adopted",
        summary=(
            "Fleet adoption of the v13 guard refusing pass_inconclusive and "
            "transporting inconclusive"
        ),
        doc_fragment="Verify fleet adoption of the v13 protocol guard that rejects",
        verified=True,
        evidence=(
            "models.py strict two-outcome guard; every reporting worker "
            "announces builtin 13 (2026-09-13 board review)"
        ),
    ),
    ActivationPrerequisite(
        key="deadline_finalizer_released",
        summary=(
            "Deadline finalizer terminates unresolved v13 processing states "
            "with the failure domain and retry evidence (Platform ships it as "
            "no-fault review_timed_out)"
        ),
        doc_fragment=(
            "Implement the deadline finalizer that converts unresolved v13 processing"
        ),
        evidence=(
            "ditto.api_server.review_timeout_finalizer; released and "
            "live-verified: pending"
        ),
    ),
    ActivationPrerequisite(
        key="ceiling_raise_after_verification",
        summary=(
            "Ceiling raised from v12 only after the finalizer and every "
            "prerequisite above are released and verified"
        ),
        doc_fragment=(
            "Raise the separately enforced screening-policy activation ceiling from v12"
        ),
        evidence=(
            "ceiling moved to v13 on 2026-09-14 by release decision (#1891) "
            "after the strict two-outcome contract shipped and both production "
            "screeners reported builtin 13 on release 0.264.0; the finalizer "
            "prerequisite above is still pending, so this item stays unverified"
        ),
    ),
    ActivationPrerequisite(
        key="rollback_preserves_evidence",
        summary=(
            "Rollback preserves evidence/revocations and never restores "
            "fail-open approval"
        ),
        doc_fragment=(
            "Rollback preserves evidence/revocations and does not restore fail-open"
        ),
    ),
)

# The last policy version that governed without this checklist. Anything above
# it may only become the activation ceiling once every prerequisite is verified.
CHECKLIST_FREE_POLICY_VERSION = STRICT_TWO_OUTCOME_POLICY_VERSION - 1


def unverified_activation_prerequisites() -> tuple[ActivationPrerequisite, ...]:
    return tuple(p for p in POLICY_V13_ACTIVATION_PREREQUISITES if not p.verified)


def activation_ceiling_from_checklist() -> int:
    """The highest policy version the checklist permits as the activation ceiling.

    Versions up to ``CHECKLIST_FREE_POLICY_VERSION`` need no checklist. The
    strict two-outcome policy and anything built on it (v14 addendum) become
    activation-ready only when every published prerequisite is verified.
    """
    if unverified_activation_prerequisites():
        return CHECKLIST_FREE_POLICY_VERSION
    return SCREENING_POLICY_VERSION


def activation_ceiling_is_consistent() -> bool:
    """True when the published ceiling constant does not outrun the checklist."""
    ceiling = SCREENING_ACTIVATION_CEILING_POLICY_VERSION
    return ceiling <= activation_ceiling_from_checklist()
