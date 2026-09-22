"""SN118 5% maintenance treasury allocation manager and budget controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

RAO_PER_TAO = 1_000_000_000
DEFAULT_TREASURY_SHARE_PCT = 0.05
DEFAULT_EPOCH_CAP_TAO = 10.0
DEFAULT_MAX_SINGLE_PAYOUT_TAO = 25.0
DEFAULT_MULTI_REVIEWER_THRESHOLD_TAO = 5.0

FundingSource = Literal["miner_vector_cut", "owner_cut"]


class TreasuryError(Exception):
    """Base exception for maintenance treasury operations."""


class TreasuryPausedError(TreasuryError):
    """Raised when an operation is attempted while disbursements are paused."""


class TreasuryUnfundedError(TreasuryError):
    """Raised when a live payout is attempted during a zero-fund rehearsal stage."""


class TreasuryBudgetExceededError(TreasuryError):
    """Raised when a payout request exceeds available epoch budget or caps."""


class MultiReviewerRequiredError(TreasuryError):
    """Raised when a payout lacks the required number of reviewer endorsements."""


class InvalidActivationStageError(TreasuryError):
    """Raised when an activation order transition violates sequential prerequisites."""


@dataclass(frozen=True)
class TreasuryAllocationConfig:
    """Governance configuration and financial boundaries for the SN118 treasury."""

    treasury_share_pct: float = DEFAULT_TREASURY_SHARE_PCT
    funding_source: FundingSource = "miner_vector_cut"
    epoch_cap_tao: float = DEFAULT_EPOCH_CAP_TAO
    max_single_payout_tao: float = DEFAULT_MAX_SINGLE_PAYOUT_TAO
    multi_reviewer_threshold_tao: float = DEFAULT_MULTI_REVIEWER_THRESHOLD_TAO
    treasury_coldkey: str = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    treasury_hotkey: str = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
    min_reviewers_for_threshold: int = 2

    @property
    def epoch_cap_rao(self) -> int:
        """Calculate maximum authorized rao disbursements per epoch."""
        return int(self.epoch_cap_tao * RAO_PER_TAO)

    @property
    def max_single_payout_rao(self) -> int:
        """Calculate maximum authorized rao disbursement for a single bounty."""
        return int(self.max_single_payout_tao * RAO_PER_TAO)

    @property
    def multi_reviewer_threshold_rao(self) -> int:
        """Calculate rao threshold requiring multiple reviewer endorsements."""
        return int(self.multi_reviewer_threshold_tao * RAO_PER_TAO)


@dataclass
class TreasuryReservation:
    """Pending fund reservation for an active, accepted, or approved bounty claim."""

    claim_id: UUID
    amount_rao: int
    reserved_at: datetime
    active: bool = True

    @property
    def amount_tao(self) -> float:
        """Convert reserved rao to TAO."""
        return self.amount_rao / RAO_PER_TAO


class TreasuryManager:
    """Manages capped treasury allocations and lifecycle activation."""

    def __init__(self, config: TreasuryAllocationConfig | None = None) -> None:
        """Initialize the treasury manager with configuration and zeroed expenditure."""
        self.config = config or TreasuryAllocationConfig()
        self.activation_stage: int = 1
        self.is_funded: bool = False
        self.emergency_paused: bool = False
        self.pause_reason: str | None = None
        self.last_resumed_by: str | None = None
        self.spent_this_epoch_rao: int = 0
        self.total_disbursed_rao: int = 0
        self.active_reservations: dict[UUID, TreasuryReservation] = {}
        self.payout_log: list[dict[str, object]] = []

    def set_activation_stage(self, stage: int) -> None:
        """Advance the formal activation order stage enforcing sequential ordering."""
        if stage < 1 or stage > 5:
            raise InvalidActivationStageError(f"Invalid activation stage: {stage}")
        if stage > self.activation_stage + 1:
            raise InvalidActivationStageError(
                f"Cannot jump from stage {self.activation_stage} to {stage}"
            )
        self.activation_stage = stage
        if stage >= 4:
            self.is_funded = True

    def activate_capped_treasury(self, epoch_cap_tao: float | None = None) -> None:
        """Activate Stage 4 capped allocation with a reversible expenditure ceiling."""
        if self.activation_stage < 3:
            raise InvalidActivationStageError(
                "Cannot activate capped treasury before completing Stage 3 rehearsal"
            )
        if epoch_cap_tao is not None:
            self.config = TreasuryAllocationConfig(
                treasury_share_pct=self.config.treasury_share_pct,
                funding_source=self.config.funding_source,
                epoch_cap_tao=epoch_cap_tao,
                max_single_payout_tao=self.config.max_single_payout_tao,
                multi_reviewer_threshold_tao=self.config.multi_reviewer_threshold_tao,
                treasury_coldkey=self.config.treasury_coldkey,
                treasury_hotkey=self.config.treasury_hotkey,
                min_reviewers_for_threshold=self.config.min_reviewers_for_threshold,
            )
        self.set_activation_stage(4)
        self.is_funded = True

    def emergency_pause(self, reason: str, operator_hotkey: str) -> None:
        """Immediately halt all treasury reservations and disbursements."""
        self.emergency_paused = True
        self.pause_reason = f"Paused by {operator_hotkey}: {reason}"

    def resume(self, operator_hotkey: str) -> None:
        """Clear emergency pause flag and resume standard treasury operations."""
        self.emergency_paused = False
        self.pause_reason = None
        self.last_resumed_by = operator_hotkey

    def reserve_funds(self, claim_id: UUID, amount_rao: int) -> TreasuryReservation:
        """Reserve allocation for a claim against the current epoch budget."""
        if self.emergency_paused:
            raise TreasuryPausedError(f"Treasury is paused: {self.pause_reason}")
        if amount_rao > self.config.max_single_payout_rao:
            raise TreasuryBudgetExceededError(
                f"Requested amount ({amount_rao} rao) exceeds maximum single payout "
                f"cap ({self.config.max_single_payout_rao} rao)"
            )
        if self.is_funded:
            projected = self.spent_this_epoch_rao + amount_rao
            rem = self.config.epoch_cap_rao - self.spent_this_epoch_rao
            if projected > self.config.epoch_cap_rao:
                raise TreasuryBudgetExceededError(
                    f"Requested reservation ({amount_rao} rao) exceeds available epoch "
                    f"budget ({rem} rao remaining)"
                )
        reservation = TreasuryReservation(
            claim_id=claim_id,
            amount_rao=amount_rao,
            reserved_at=datetime.now(UTC),
            active=True,
        )
        self.active_reservations[claim_id] = reservation
        return reservation

    def release_reservation(self, claim_id: UUID) -> None:
        """Release a pending fund reservation when a claim expires or is cancelled."""
        if claim_id in self.active_reservations:
            self.active_reservations[claim_id].active = False
            del self.active_reservations[claim_id]

    def validate_payout_request(
        self,
        amount_rao: int,
        reviewer_count: int,
        is_rehearsal: bool = False,
    ) -> None:
        """Validate whether payout complies with budget caps and review thresholds."""
        if self.emergency_paused:
            raise TreasuryPausedError(f"Treasury is paused: {self.pause_reason}")
        if not self.is_funded and not is_rehearsal:
            raise TreasuryUnfundedError(
                "Treasury is not funded; live payouts require Stage 4 activation"
            )
        if amount_rao > self.config.max_single_payout_rao:
            raise TreasuryBudgetExceededError(
                f"Payout amount ({amount_rao} rao) exceeds single payout ceiling "
                f"({self.config.max_single_payout_rao} rao)"
            )
        threshold_rao = self.config.multi_reviewer_threshold_rao
        min_revs = self.config.min_reviewers_for_threshold
        if amount_rao >= threshold_rao and reviewer_count < min_revs:
            raise MultiReviewerRequiredError(
                f"Payout of {amount_rao} rao exceeds multi-reviewer threshold "
                f"({threshold_rao} rao) and requires at least {min_revs} reviewers"
            )
        if (
            self.is_funded
            and not is_rehearsal
            and self.spent_this_epoch_rao + amount_rao > self.config.epoch_cap_rao
        ):
            rem = self.config.epoch_cap_rao - self.spent_this_epoch_rao
            raise TreasuryBudgetExceededError(
                f"Payout amount ({amount_rao} rao) exceeds remaining epoch capacity "
                f"({rem} rao remaining)"
            )

    def record_payout(
        self,
        claim_id: UUID,
        amount_rao: int,
        payee_coldkey: str,
        extrinsic_hash: str,
        is_rehearsal: bool = False,
    ) -> dict[str, object]:
        """Record an executed payout and update epoch accounting records."""
        now = datetime.now(UTC)
        if not is_rehearsal:
            self.spent_this_epoch_rao += amount_rao
            self.total_disbursed_rao += amount_rao
        if claim_id in self.active_reservations:
            self.active_reservations[claim_id].active = False
            del self.active_reservations[claim_id]
        record = {
            "claim_id": str(claim_id),
            "amount_rao": amount_rao,
            "amount_tao": amount_rao / RAO_PER_TAO,
            "payee_coldkey": payee_coldkey,
            "extrinsic_hash": extrinsic_hash,
            "is_rehearsal": is_rehearsal,
            "timestamp": now.isoformat(),
        }
        self.payout_log.append(record)
        return record

    def reset_epoch(self) -> None:
        """Reset the epoch expenditure accumulator for the next accounting cycle."""
        self.spent_this_epoch_rao = 0
