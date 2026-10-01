"""Operator-revisioned scoring lease clocks (ditto-subnet #1156).

The canonical scoring ticket TTL used to be a Platform constant
(``_TICKET_TTL`` in ``ditto.api_server.endpoints.validator``), paired by hand
with ``REPLACEMENT_TICKET_TTL`` for score re-tests. Every retune -- 430 -> 180
minutes in #1154 was the latest -- therefore needed a deploy. These models back
an append-only revision table so an operator can move it from Backroom, next to
the neighbouring capacity knobs (case concurrency, validator slots, inference
budgets).

One field governs both clocks, so they cannot drift apart: a canonical lease and
the replacement lease for the same scored work run the same benchmark under the
same validator budget.

**New leases only.** The TTL is stamped onto a ticket's ``deadline`` when the
ticket is minted, and the resume path returns a live ticket untouched. A lower
TTL never rewrites a live deadline; a higher one never extends one.

The bounds are operational, not cosmetic:

* The validator's run budget is ``min(harness cap, lease - report margin)``, so
  a TTL under its 165-minute harness cap still binds the fleet with no
  validator release. The floor keeps that budget above the observed 8-wide
  v11 completion maximum (60 minutes).
* The ceiling is the validator Compose ``stop_grace_period`` / updater drain
  (245 minutes, sized to the 4-hour confirmation lease) minus five minutes for
  the signed terminal report. A Backroom raise above it would let a restart
  SIGKILL live work, so the Platform refuses it rather than trusting the drain
  to follow. It also stays far under the 8-hour broker session cap.

Each revision carries the COMPLETE policy (never a diff), matching the other
settings boards, so the object can grow the rest of the #1156 clock family
without a partial write ever resetting a field the operator did not name.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_SCORING_TICKET_TTL_MINUTES = 180
"""The pre-#1156 ``_TICKET_TTL`` constant. An empty revision table resolves to
exactly this, so the deploy that introduces the board changes no lease.

It stays longer than the validator's 165-minute benchmark cap; the remaining
fifteen minutes cover artifact/setup time and the validator's two-minute signed
report margin. Production v11 canonical completions (21d): 8-wide p99 55 min /
max 60 min; including serial windows p99 101 min / max 114 min. Zero of 1014
scored tickets exceeded 120 min. 180 is over-budgeted for that tail without the
430-minute silent occupancy of a wedged scorer."""

DEFAULT_SCORING_TICKET_TTL = timedelta(minutes=DEFAULT_SCORING_TICKET_TTL_MINUTES)

MIN_SCORING_TICKET_TTL_MINUTES = 60
"""Production v11 8-wide completions peak at 60 minutes. Below that the lease,
not the agent, decides the outcome of an ordinary run."""

VALIDATOR_DRAIN_MINUTES = 245
"""Validator Compose ``stop_grace_period`` and both updater drain defaults."""

SIGNED_REPORT_MARGIN_MINUTES = 5
"""Room the drain leaves after the longest lease for the signed final report."""

MAX_SCORING_TICKET_TTL_MINUTES = VALIDATOR_DRAIN_MINUTES - SIGNED_REPORT_MARGIN_MINUTES
"""240 minutes: never longer than a validator restart will wait for."""

SCORING_LEASE_SETTINGS_SCOPE = "*"


def scoring_lease_confirmation(scoring_ticket_ttl_minutes: int) -> str:
    """The phrase a write must carry, naming the TTL this revision applies."""
    return f"APPLY SCORING TICKET TTL {scoring_ticket_ttl_minutes} MINUTES"


class ScoringLeaseSettings(BaseModel):
    """The complete scoring-lease clock policy."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    scoring_ticket_ttl_minutes: Annotated[
        int,
        Field(ge=MIN_SCORING_TICKET_TTL_MINUTES, le=MAX_SCORING_TICKET_TTL_MINUTES),
    ] = DEFAULT_SCORING_TICKET_TTL_MINUTES
    """Deadline stamped on every NEW canonical, rollout, backfill, carryover,
    continual-retest, and benchmark-canary scoring ticket, and on every new
    score-retest replacement ticket."""

    @property
    def scoring_ticket_ttl(self) -> timedelta:
        return timedelta(minutes=self.scoring_ticket_ttl_minutes)


class ScoringLeaseSettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    scope: str
    settings: ScoringLeaseSettings
    reason: str
    actor: str
    created_at: datetime
    checksum: str
    settings_valid: bool = True
    """False means settings shows the default fallback, not the stored policy.

    The checksum and audit fields still identify the original stored revision.
    """


class EffectiveScoringLeaseSettings(BaseModel):
    """What the next minted lease will use, and where it came from."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    scope: str
    settings: ScoringLeaseSettings
    checksum: str
    source: Literal["revision", "default"]
    settings_valid: bool = True
    min_scoring_ticket_ttl_minutes: int = MIN_SCORING_TICKET_TTL_MINUTES
    max_scoring_ticket_ttl_minutes: int = MAX_SCORING_TICKET_TTL_MINUTES
    max_age_seconds: float
    """Upper bound on how long a write takes to reach ticket issuance."""


class AdminScoringLeaseSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    scope: str = SCORING_LEASE_SETTINGS_SCOPE
    expected_revision: Annotated[int, Field(ge=0)]
    settings: ScoringLeaseSettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @model_validator(mode="after")
    def _require_complete_policy(self) -> AdminScoringLeaseSettingsRequest:
        """Reject a partial policy on a whole-object store.

        Every field has a default so an empty board is the shipped policy. On a
        write that default would silently replace a field the operator did not
        send, so every field must be explicit.
        """
        missing = sorted(
            set(ScoringLeaseSettings.model_fields) - self.settings.model_fields_set
        )
        if missing:
            raise ValueError(
                "a scoring lease revision stores the WHOLE policy, so every field "
                f"must be sent explicitly; missing {missing}. Read "
                "GET /admin/scoring-lease-settings and send back the complete object."
            )
        return self


class AdminScoringLeaseSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: list[ScoringLeaseSettingsRevision]
    history: list[ScoringLeaseSettingsRevision]
    default: ScoringLeaseSettings
    effective: EffectiveScoringLeaseSettings
