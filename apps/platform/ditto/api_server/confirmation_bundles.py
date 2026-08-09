"""Pure policy core for bounded Bench v9 confirmation bundles.

This module deliberately has no FastAPI, SQLAlchemy, or clock dependencies.  It
defines the decisions that later API/database layers persist under locks:

* base-axis candidate selection and confirmed-frontier ranking;
* immutable, checksummed operator settings;
* artifact/profile/retest deduplication identities;
* confirmation lifecycle transitions; and
* compare-and-swap friendly UTC daily budget arithmetic.

Keeping these rules pure makes the expensive lane independently testable before
its schema, endpoints, and worker protocol exist.  Persistence remains the
authority for concurrency: callers must compare ``DailyBudgetSnapshot.revision``
while locking the row for ``utc_day`` before applying a returned snapshot.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal

DEFAULT_TOP_N = 5
MAX_TOP_N = 10
MAX_DAILY_BUNDLE_CAP = 1_000
MAX_DAILY_DOLLAR_MICROUSD = 1_000_000_000
MAX_BUNDLE_REQUEST_CAP = 100_000
MAX_BUNDLE_TOKEN_CAP = 100_000_000
DEFAULT_CHALLENGER_Z = 1.64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ConfirmationPolicyError(ValueError):
    """An immutable policy value or state transition is invalid."""


class BudgetExhausted(ConfirmationPolicyError):
    """A new confirmation attempt cannot be reserved under the daily limits."""

    def __init__(self, reason: Literal["bundle_cap", "dollar_cap"]) -> None:
        self.reason = reason
        super().__init__(reason)


class StaleBudgetSnapshot(ConfirmationPolicyError):
    """The caller tried to apply a decision based on an old budget revision."""


class ConfirmationMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    ENFORCE = "enforce"


class ConfirmationState(StrEnum):
    BASE_ONLY = "base_only"
    BLOCKED_BUDGET = "blocked_budget"
    PENDING = "pending"
    LEASED = "leased"
    FAILED = "failed"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"


class ReservationState(StrEnum):
    RESERVED = "reserved"
    SETTLED = "settled"


@dataclass(frozen=True)
class ConfirmationProfileRef:
    """Immutable identity of the full confirmation contract."""

    revision: str
    checksum: str

    def __post_init__(self) -> None:
        if not self.revision or len(self.revision) > 128:
            raise ConfirmationPolicyError("profile revision must contain 1-128 chars")
        _require_sha256(self.checksum, "profile checksum")


@dataclass(frozen=True)
class ConfirmationBundleSettings:
    """Complete operator policy for issuing expensive confirmation attempts."""

    mode: ConfirmationMode = ConfirmationMode.OFF
    top_n: int = DEFAULT_TOP_N
    daily_bundle_cap: int = 0
    daily_dollar_cap_microusd: int = 0
    per_bundle_request_cap: int = 1
    per_bundle_token_cap: int = 1
    profile: ConfirmationProfileRef = field(
        default_factory=lambda: ConfirmationProfileRef(
            revision="unconfigured",
            checksum="0" * 64,
        )
    )
    challenger_z: float = DEFAULT_CHALLENGER_Z

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ConfirmationMode):
            raise ConfirmationPolicyError("mode must be a ConfirmationMode")
        _require_int_between("top_n", self.top_n, 1, MAX_TOP_N)
        _require_int_between(
            "daily_bundle_cap", self.daily_bundle_cap, 0, MAX_DAILY_BUNDLE_CAP
        )
        _require_int_between(
            "daily_dollar_cap_microusd",
            self.daily_dollar_cap_microusd,
            0,
            MAX_DAILY_DOLLAR_MICROUSD,
        )
        _require_int_between(
            "per_bundle_request_cap",
            self.per_bundle_request_cap,
            1,
            MAX_BUNDLE_REQUEST_CAP,
        )
        _require_int_between(
            "per_bundle_token_cap",
            self.per_bundle_token_cap,
            1,
            MAX_BUNDLE_TOKEN_CAP,
        )
        _require_finite_between(
            "challenger_z", self.challenger_z, minimum=0.0, maximum=3.0
        )
        if self.mode != ConfirmationMode.OFF and (
            self.daily_bundle_cap == 0 or self.daily_dollar_cap_microusd == 0
        ):
            raise ConfirmationPolicyError(
                "shadow/enforce mode requires positive daily bundle and dollar caps"
            )

    @property
    def checksum(self) -> str:
        return settings_checksum(self)


@dataclass(frozen=True)
class CheckedSettings:
    """Settings plus the checksum supplied by persistence or an API caller."""

    settings: ConfirmationBundleSettings
    checksum: str

    def __post_init__(self) -> None:
        _require_sha256(self.checksum, "settings checksum")
        if self.checksum != settings_checksum(self.settings):
            raise ConfirmationPolicyError("settings checksum does not match payload")

    @classmethod
    def create(cls, settings: ConfirmationBundleSettings) -> CheckedSettings:
        return cls(settings=settings, checksum=settings_checksum(settings))


@dataclass(frozen=True)
class ConfirmationCandidate:
    """One submission on the comparable base-v9 score axis."""

    agent_id: str
    owner_id: str
    artifact_sha256: str
    bench_version: int
    base_composite: float
    base_stderr: float | None
    first_seen: datetime
    confirmation_state: ConfirmationState = ConfirmationState.BASE_ONLY
    full_composite: float | None = None

    def __post_init__(self) -> None:
        if not self.agent_id or not self.owner_id:
            raise ConfirmationPolicyError("candidate agent and owner ids are required")
        _require_sha256(self.artifact_sha256, "artifact sha256")
        _require_int_between("bench_version", self.bench_version, 1, 1_000_000)
        _require_finite_between(
            "base_composite", self.base_composite, minimum=0.0, maximum=1.0
        )
        if self.base_stderr is not None:
            _require_finite_between(
                "base_stderr", self.base_stderr, minimum=0.0, maximum=1.0
            )
        if self.full_composite is not None:
            _require_finite_between(
                "full_composite", self.full_composite, minimum=0.0, maximum=1.0
            )
        if self.first_seen.tzinfo is None:
            raise ConfirmationPolicyError("first_seen must be timezone-aware")
        if not isinstance(self.confirmation_state, ConfirmationState):
            raise ConfirmationPolicyError(
                "confirmation_state must be a ConfirmationState"
            )


@dataclass(frozen=True)
class CandidateSelection:
    """Deterministic confirmation cohort plus the cutoff used to form it."""

    base_board: tuple[ConfirmationCandidate, ...]
    confirmed_board: tuple[ConfirmationCandidate, ...]
    selected: tuple[ConfirmationCandidate, ...]
    top_n_agent_ids: frozenset[str]
    challenger_agent_ids: frozenset[str]
    missing_uncertainty_agent_ids: frozenset[str]
    cutoff: ConfirmationCandidate | None
    cutoff_source: Literal["confirmed_frontier", "base_frontier", "none"]

    @property
    def pending(self) -> tuple[ConfirmationCandidate, ...]:
        return tuple(c for c in self.selected if not full_confirmation_eligible(c))


@dataclass(frozen=True)
class ConfirmationBundleKey:
    """The exact cache and retry identity for one artifact confirmation."""

    artifact_sha256: str
    bench_version: int
    profile_revision: str
    profile_checksum: str
    retest_generation: int = 0

    def __post_init__(self) -> None:
        _require_sha256(self.artifact_sha256, "artifact sha256")
        _require_sha256(self.profile_checksum, "profile checksum")
        _require_int_between("bench_version", self.bench_version, 1, 1_000_000)
        _require_int_between("retest_generation", self.retest_generation, 0, 1_000_000)
        if not self.profile_revision or len(self.profile_revision) > 128:
            raise ConfirmationPolicyError("profile revision must contain 1-128 chars")

    @classmethod
    def for_candidate(
        cls,
        candidate: ConfirmationCandidate,
        profile: ConfirmationProfileRef,
        *,
        retest_generation: int = 0,
    ) -> ConfirmationBundleKey:
        return cls(
            artifact_sha256=candidate.artifact_sha256,
            bench_version=candidate.bench_version,
            profile_revision=profile.revision,
            profile_checksum=profile.checksum,
            retest_generation=retest_generation,
        )


@dataclass(frozen=True)
class DailyBudgetSnapshot:
    """One lockable UTC-day accounting row."""

    utc_day: date
    revision: int = 0
    issued_attempts: int = 0
    outstanding_reserved_microusd: int = 0
    settled_microusd: int = 0

    def __post_init__(self) -> None:
        for name in (
            "revision",
            "issued_attempts",
            "outstanding_reserved_microusd",
            "settled_microusd",
        ):
            _require_int_between(name, getattr(self, name), 0, 1 << 62)

    @property
    def committed_microusd(self) -> int:
        return self.outstanding_reserved_microusd + self.settled_microusd

    @classmethod
    def for_time(cls, now: datetime) -> DailyBudgetSnapshot:
        return cls(utc_day=utc_day(now))


@dataclass(frozen=True)
class BudgetReservation:
    """One issued attempt's immutable reservation identity and settlement."""

    reservation_id: str
    bundle_key: ConfirmationBundleKey
    utc_day: date
    reserved_microusd: int
    state: ReservationState = ReservationState.RESERVED
    actual_microusd: int | None = None
    failed_attempt: bool | None = None

    def __post_init__(self) -> None:
        if not self.reservation_id:
            raise ConfirmationPolicyError("reservation id is required")
        _require_int_between("reserved_microusd", self.reserved_microusd, 1, 1 << 62)
        if self.actual_microusd is not None:
            _require_int_between("actual_microusd", self.actual_microusd, 0, 1 << 62)
        if self.state == ReservationState.RESERVED and (
            self.actual_microusd is not None or self.failed_attempt is not None
        ):
            raise ConfirmationPolicyError(
                "an unsettled reservation cannot carry settlement fields"
            )
        if self.state == ReservationState.SETTLED and (
            self.actual_microusd is None or self.failed_attempt is None
        ):
            raise ConfirmationPolicyError(
                "a settled reservation requires actual cost and outcome"
            )


def settings_checksum(settings: ConfirmationBundleSettings) -> str:
    """Return the canonical checksum for a complete settings payload."""
    payload = asdict(settings)
    payload["mode"] = settings.mode.value
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def full_confirmation_eligible(candidate: ConfirmationCandidate) -> bool:
    """Whether a v9 row has complete evidence for simulated full ranking."""
    return (
        candidate.bench_version == 9
        and candidate.confirmation_state == ConfirmationState.COMPLETED
        and candidate.full_composite is not None
        and math.isfinite(candidate.full_composite)
        and 0.0 <= candidate.full_composite <= 1.0
    )


def reward_eligible(
    candidate: ConfirmationCandidate, *, mode: ConfirmationMode
) -> bool:
    """Whether active policy lets this row enter authoritative rewards.

    Off/shadow preserve the pre-enforcement base ranking while evidence is
    measured. Enforce is the only mode that replaces that authority with the
    fail-closed full-confirmation gate. Pre-v9 contracts are unchanged.
    """
    if candidate.bench_version != 9 or mode != ConfirmationMode.ENFORCE:
        return True
    return full_confirmation_eligible(candidate)


def select_confirmation_candidates(
    candidates: Iterable[ConfirmationCandidate],
    *,
    top_n: int = DEFAULT_TOP_N,
    challenger_z: float = DEFAULT_CHALLENGER_Z,
) -> CandidateSelection:
    """Select owner-grouped base leaders and uncertainty-bound challengers.

    The confirmed frontier is ranked by full composite, but its cutoff is read
    on the base axis.  A base-only score is never compared numerically with a
    full score.  Before ``top_n`` owners are confirmed, the Nth base owner is a
    provisional cutoff so an exact tie is not split by the deterministic age
    tiebreak.
    """
    _require_int_between("top_n", top_n, 1, MAX_TOP_N)
    _require_finite_between("challenger_z", challenger_z, minimum=0.0, maximum=3.0)
    rows = tuple(candidates)
    versions = {row.bench_version for row in rows}
    if len(versions) > 1:
        raise ConfirmationPolicyError("candidate selection cannot mix bench versions")
    if versions and versions != {9}:
        raise ConfirmationPolicyError("confirmation candidates must use bench v9")

    base_board = _group_best(rows, score_axis="base")
    confirmed_board = _group_best(
        (row for row in rows if full_confirmation_eligible(row)), score_axis="full"
    )
    top = base_board[:top_n]
    top_ids = frozenset(row.agent_id for row in top)

    if len(confirmed_board) >= top_n:
        cutoff = confirmed_board[top_n - 1]
        cutoff_source: Literal["confirmed_frontier", "base_frontier", "none"] = (
            "confirmed_frontier"
        )
    elif len(base_board) >= top_n:
        cutoff = base_board[top_n - 1]
        cutoff_source = "base_frontier"
    else:
        cutoff = None
        cutoff_source = "none"

    challengers: list[ConfirmationCandidate] = []
    missing_uncertainty: set[str] = set()
    if cutoff is not None:
        for candidate in base_board[top_n:]:
            crosses, missing = plausibly_crosses_base_cutoff(
                candidate, cutoff, challenger_z=challenger_z
            )
            if crosses:
                challengers.append(candidate)
            elif missing:
                missing_uncertainty.add(candidate.agent_id)

    selected = (*top, *challengers)
    return CandidateSelection(
        base_board=base_board,
        confirmed_board=confirmed_board,
        selected=selected,
        top_n_agent_ids=top_ids,
        challenger_agent_ids=frozenset(row.agent_id for row in challengers),
        missing_uncertainty_agent_ids=frozenset(missing_uncertainty),
        cutoff=cutoff,
        cutoff_source=cutoff_source,
    )


def plausibly_crosses_base_cutoff(
    candidate: ConfirmationCandidate,
    cutoff: ConfirmationCandidate,
    *,
    challenger_z: float,
) -> tuple[bool, bool]:
    """Return ``(crosses, missing_required_uncertainty)`` on the base axis."""
    if candidate.bench_version != cutoff.bench_version:
        raise ConfirmationPolicyError("cutoff comparison cannot mix bench versions")
    _require_finite_between("challenger_z", challenger_z, minimum=0.0, maximum=3.0)
    gap = cutoff.base_composite - candidate.base_composite
    if gap <= 0.0:
        return True, False
    if candidate.base_stderr is None or cutoff.base_stderr is None:
        return False, True
    tolerance = challenger_z * math.sqrt(
        candidate.base_stderr**2 + cutoff.base_stderr**2
    )
    return gap <= tolerance, False


def reusable_completed_evidence(
    existing_key: ConfirmationBundleKey,
    existing_state: ConfirmationState,
    requested_key: ConfirmationBundleKey,
) -> bool:
    """Whether an exact frozen-profile result can satisfy another submission."""
    return (
        existing_state == ConfirmationState.COMPLETED and existing_key == requested_key
    )


def authorize_retest(
    key: ConfirmationBundleKey,
    state: ConfirmationState,
    *,
    operator_authorized: bool,
) -> ConfirmationBundleKey:
    """Create the next non-reusable generation without mutating old evidence."""
    if not operator_authorized:
        raise ConfirmationPolicyError("confirmation retest requires operator audit")
    if state != ConfirmationState.COMPLETED:
        raise ConfirmationPolicyError("only completed evidence can be superseded")
    return replace(key, retest_generation=key.retest_generation + 1)


_ALLOWED_TRANSITIONS: dict[ConfirmationState, frozenset[ConfirmationState]] = {
    ConfirmationState.BASE_ONLY: frozenset(
        {ConfirmationState.PENDING, ConfirmationState.BLOCKED_BUDGET}
    ),
    ConfirmationState.BLOCKED_BUDGET: frozenset({ConfirmationState.PENDING}),
    ConfirmationState.PENDING: frozenset(
        {ConfirmationState.LEASED, ConfirmationState.BLOCKED_BUDGET}
    ),
    ConfirmationState.LEASED: frozenset(
        {ConfirmationState.COMPLETED, ConfirmationState.FAILED}
    ),
    ConfirmationState.FAILED: frozenset({ConfirmationState.PENDING}),
    ConfirmationState.COMPLETED: frozenset({ConfirmationState.SUPERSEDED}),
    ConfirmationState.SUPERSEDED: frozenset(),
}


def transition_confirmation_state(
    current: ConfirmationState, target: ConfirmationState
) -> ConfirmationState:
    """Validate one lifecycle transition and return the immutable new state."""
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ConfirmationPolicyError(
            f"invalid confirmation transition {current.value} -> {target.value}"
        )
    return target


def utc_day(now: datetime) -> date:
    """Resolve an aware timestamp to its accounting day in UTC."""
    if now.tzinfo is None:
        raise ConfirmationPolicyError("budget time must be timezone-aware")
    return now.astimezone(UTC).date()


def reserve_daily_budget(
    snapshot: DailyBudgetSnapshot,
    *,
    expected_revision: int,
    settings: ConfirmationBundleSettings,
    bundle_key: ConfirmationBundleKey,
    reservation_id: str,
    reserve_microusd: int,
) -> tuple[DailyBudgetSnapshot, BudgetReservation]:
    """Reserve one attempt using compare-and-swap friendly integer arithmetic."""
    if expected_revision != snapshot.revision:
        raise StaleBudgetSnapshot(
            f"budget revision changed: expected {expected_revision}, "
            f"found {snapshot.revision}"
        )
    if settings.mode == ConfirmationMode.OFF:
        raise ConfirmationPolicyError("confirmation bundle policy is off")
    _require_int_between("reserve_microusd", reserve_microusd, 1, 1 << 62)
    if snapshot.issued_attempts + 1 > settings.daily_bundle_cap:
        raise BudgetExhausted("bundle_cap")
    if (
        snapshot.committed_microusd + reserve_microusd
        > settings.daily_dollar_cap_microusd
    ):
        raise BudgetExhausted("dollar_cap")
    updated = replace(
        snapshot,
        revision=snapshot.revision + 1,
        issued_attempts=snapshot.issued_attempts + 1,
        outstanding_reserved_microusd=(
            snapshot.outstanding_reserved_microusd + reserve_microusd
        ),
    )
    reservation = BudgetReservation(
        reservation_id=reservation_id,
        bundle_key=bundle_key,
        utc_day=snapshot.utc_day,
        reserved_microusd=reserve_microusd,
    )
    return updated, reservation


def settle_daily_budget(
    snapshot: DailyBudgetSnapshot,
    reservation: BudgetReservation,
    *,
    expected_revision: int,
    actual_microusd: int,
    failed_attempt: bool,
) -> tuple[DailyBudgetSnapshot, BudgetReservation]:
    """Settle accepted evidence/failure even when actual spend exceeds the cap.

    Limits gate *new issuance*.  They never reject a terminal result from an
    already-issued lease.  Consequently settlement may move ``settled_microusd``
    over today's configured limit; the next reservation will fail closed.
    """
    if expected_revision != snapshot.revision:
        raise StaleBudgetSnapshot(
            f"budget revision changed: expected {expected_revision}, "
            f"found {snapshot.revision}"
        )
    if reservation.utc_day != snapshot.utc_day:
        raise ConfirmationPolicyError(
            "reservation must settle against its original UTC-day row"
        )
    if reservation.state != ReservationState.RESERVED:
        raise ConfirmationPolicyError("reservation was already settled")
    if snapshot.outstanding_reserved_microusd < reservation.reserved_microusd:
        raise ConfirmationPolicyError("budget snapshot is missing the reservation")
    _require_int_between("actual_microusd", actual_microusd, 0, 1 << 62)
    updated = replace(
        snapshot,
        revision=snapshot.revision + 1,
        outstanding_reserved_microusd=(
            snapshot.outstanding_reserved_microusd - reservation.reserved_microusd
        ),
        settled_microusd=snapshot.settled_microusd + actual_microusd,
    )
    settled = replace(
        reservation,
        state=ReservationState.SETTLED,
        actual_microusd=actual_microusd,
        failed_attempt=failed_attempt,
    )
    return updated, settled


def _group_best(
    candidates: Iterable[ConfirmationCandidate],
    *,
    score_axis: Literal["base", "full"],
) -> tuple[ConfirmationCandidate, ...]:
    by_owner: dict[str, ConfirmationCandidate] = {}
    for candidate in candidates:
        current = by_owner.get(candidate.owner_id)
        if current is None or _candidate_sort_key(candidate, score_axis) < (
            _candidate_sort_key(current, score_axis)
        ):
            by_owner[candidate.owner_id] = candidate
    return tuple(
        sorted(by_owner.values(), key=lambda row: _candidate_sort_key(row, score_axis))
    )


def _candidate_sort_key(
    candidate: ConfirmationCandidate, score_axis: Literal["base", "full"]
) -> tuple[float, datetime, str]:
    score = (
        candidate.base_composite
        if score_axis == "base"
        else (
            candidate.full_composite if candidate.full_composite is not None else -1.0
        )
    )
    return (-score, candidate.first_seen.astimezone(UTC), candidate.agent_id)


def _require_sha256(value: str, label: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ConfirmationPolicyError(f"{label} must be 64 lowercase hex chars")


def _require_int_between(name: str, value: int, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfirmationPolicyError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfirmationPolicyError(f"{name} must be between {minimum} and {maximum}")


def _require_finite_between(
    name: str, value: float, *, minimum: float, maximum: float
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfirmationPolicyError(f"{name} must be numeric")
    if not math.isfinite(float(value)) or not minimum <= float(value) <= maximum:
        raise ConfirmationPolicyError(
            f"{name} must be finite and between {minimum} and {maximum}"
        )


__all__ = [
    "BudgetExhausted",
    "BudgetReservation",
    "CandidateSelection",
    "CheckedSettings",
    "ConfirmationBundleKey",
    "ConfirmationBundleSettings",
    "ConfirmationCandidate",
    "ConfirmationMode",
    "ConfirmationPolicyError",
    "ConfirmationProfileRef",
    "ConfirmationState",
    "DailyBudgetSnapshot",
    "MAX_TOP_N",
    "ReservationState",
    "StaleBudgetSnapshot",
    "authorize_retest",
    "full_confirmation_eligible",
    "plausibly_crosses_base_cutoff",
    "reserve_daily_budget",
    "reusable_completed_evidence",
    "reward_eligible",
    "select_confirmation_candidates",
    "settings_checksum",
    "settle_daily_budget",
    "transition_confirmation_state",
    "utc_day",
]
