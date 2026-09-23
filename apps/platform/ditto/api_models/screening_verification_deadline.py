"""Read-only v13 verification-deadline and finalizer-state visibility (#2100).

Policy v13 requires an exact-artifact deadline and retry/failure-domain record
before a held submission can receive a non-misconduct V1/V2/V3 rejection. The
published 24-hour window (``workers/screener/docs/policy-v13.md``) is a
recommended default, not evidence of a currently deployed cutoff -- and the
attempt-level deadline Backroom already shows (``ScreeningAttempt.deadline``)
is a screening-lease expiry, never the policy's verification deadline.

``GET /admin/screening-verification-deadline/{agent_id}`` answers, for one
exact agent, the question PR #1871's finalizer raises but does not itself
expose: is there a live artifact-level deadline for this hold, has it passed,
and would the finalizer (in whatever posture it is configured) treat this row
as ready to terminate? It never mutates and never infers a deadline the
platform has not actually computed from a stored quarantine.

``finalizer_state`` is deliberately exactly the four values the issue names:
``pending`` | ``ready`` | ``finalized`` | ``not_configured``. Every situation
where the deadline finalizer simply does not apply to this row -- global mode
``off``, no active quarantine, an operator-finding hold (never a processing
state the finalizer can touch), or a policy version older than the finalizer's
floor -- reads as ``not_configured`` with ``not_applicable_reason`` naming
exactly which of those it is; ``is_operator_finding_hold`` additionally flags
the finding case on its own so a caller can filter for it without string
matching. This keeps the enum small and stable while still being fully
explicit about *why* no live deadline governs a given row -- never a
5th enum value, never a silently reused meaning.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.screening_decision import ScreeningDecisionRecordView
from ditto_screening_protocol import FailureDomain, ReviewTimeoutFinalizerMode

ScreeningFailureDomain: TypeAlias = FailureDomain

FinalizerState: TypeAlias = Literal["not_configured", "pending", "ready", "finalized"]

# Why `finalizer_state` reads `not_configured` for this exact row. Absent
# (null) whenever finalizer_state is pending, ready, or finalized.
NotApplicableReason: TypeAlias = Literal[
    "finalizer_mode_off",
    "no_active_quarantine",
    "operator_finding_hold",
    "policy_version_not_covered",
    "reason_code_not_covered",
]

# The verification window has exactly one source today: the published
# ReviewTimeoutPolicy constant. No per-activation revision of it is ever
# persisted (screener_policy_activation.py stores only target_policy_version),
# so `stored_revision` is reserved for the day one is and must never be
# fabricated before that lands.
DeadlineProvenance: TypeAlias = Literal["shipped_default", "stored_revision"]


class ScreeningVerificationDeadlineView(BaseModel):
    """Effective v13 verification deadline and finalizer state for one agent."""

    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    agent_status: str

    # Immutable identity. ``artifact_sha256`` always comes from the live agent
    # row (upload-time SHA never changes for an exact agent_id); when a
    # decision already exists this is cross-checked against the SHA it bound.
    artifact_sha256: str
    artifact_identity_verified: bool | None = Field(
        default=None,
        description=(
            "Whether the artifact SHA bound into the finding or decision "
            "record still matches the live agent's SHA. Null when there is "
            "no such bound evidence yet to check (a non-decisive hold before "
            "any finding or decision exists)."
        ),
    )

    has_active_quarantine: bool
    is_operator_finding_hold: bool
    quarantine_id: UUID | None = None
    quarantine_status: Literal["active", "resolved"] | None = None
    quarantine_resolution: str | None = None
    attempt_id: UUID | None = None
    reason_code: str | None = None

    policy_version: int | None = None
    policy_covered_by_finalizer: bool
    policy_digest: str | None = None
    verification_profile_digest: str | None = None

    verification_window_start: datetime | None = None
    verification_deadline: datetime | None = None
    verification_deadline_provenance: DeadlineProvenance | None = None

    failure_domain: ScreeningFailureDomain | None = None
    required_retries: int | None = None
    recorded_retry_attempts: int | None = None
    independent_worker_hotkeys: list[str] = Field(default_factory=list)
    independent_worker_count: int = 0
    independent_worker_requirement_met: bool | None = None

    # Mirrors the decision record's own fields when one exists; null
    # pre-decision because the platform tracks no per-check completion ledger
    # for an active quarantine before a decision record is written (only the
    # published 19-item mandatory-verification list in policy-v13.md, and the
    # quarantine's own evidence/review-audit trail, which are not a
    # check-by-check completion ledger).
    completed_checks: list[str] | None = None
    failed_checks: list[str] | None = None

    finalizer_mode: ReviewTimeoutFinalizerMode
    finalizer_state: FinalizerState
    not_applicable_reason: NotApplicableReason | None = None

    # Present, and authoritative over every field above that it also carries
    # (failure_domain, retry evidence, completed/failed checks, identities),
    # exactly when finalizer_state == "finalized".
    decision: ScreeningDecisionRecordView | None = None
