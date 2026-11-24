"""Bounty acceptance, alpha awards, fee refunds, and accounting ledger (#2046).

Implements the deterministic decision and alpha accounting engine for SN118
build bounties:
- Reviewer decisions: ``accepted``, ``changes_requested`` (with bounded rework
  window), ``rejected``, and ``cancelled_by_team``.
- Acceptance validation: requires merged/submitted PR number, 40-hex commit SHA,
  passing validation output, and non-blank reviewer rationale.
- Single-winner and shared-winner awards bounded by the signed revision's
  ``reward_pool_alpha_rao``, with explicit attribution and rationale.
- Automatic entry-fee disposition:
  - ``fee_refund`` on ``accepted``
  - ``team_cancel_refund`` on ``cancelled_by_team``
  - ``fee_forfeit`` on ``rejected``, ``expired``, or ``withdrawn`` once finalized
- Duplicate-payout prevention and operator recusal on approval/settlement.
- Full per-bounty accounting summary reconstruction (total fees collected,
  fees refunded, fees retained/forfeited, alpha awarded, and escrow balance).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

DecisionType = Literal[
    "accepted",
    "changes_requested",
    "rejected",
    "cancelled_by_team",
]

AccountingReason = Literal[
    "bounty_award",
    "fee_refund",
    "fee_forfeit",
    "team_cancel_refund",
]

AttemptStatus = Literal[
    "claimed",
    "submitted",
    "rework",
    "accepted",
    "rejected",
    "expired",
    "withdrawn",
    "cancelled_by_team",
]


@dataclass
class AttemptRecord:
    claim_id: UUID
    bounty_id: UUID
    revision: int
    hotkey: str
    payee_coldkey: str
    github_login: str
    entry_fee_alpha_rao: int
    reward_pool_alpha_rao: int
    fee_extrinsic_ref: str
    status: AttemptStatus = "claimed"
    pr_number: int | None = None
    head_sha: str | None = None
    validation_output: str | None = None
    reviewer_actor: str | None = None
    reviewer_rationale: str | None = None
    attribution: str | None = None
    rework_deadline_at: datetime | None = None
    decided_at: datetime | None = None


@dataclass(frozen=True)
class AccountingLedgerEntry:
    entry_id: UUID
    bounty_id: UUID
    claim_id: UUID
    recipient_coldkey: str
    recipient_hotkey: str
    amount_alpha_rao: int
    reason: AccountingReason
    extrinsic_ref: str
    operator_approvals: tuple[str, ...]
    attribution: str | None
    rationale: str
    recorded_at: datetime


@dataclass(frozen=True)
class BountyAccountingSummary:
    bounty_id: UUID
    reward_pool_alpha_rao: int
    total_fees_collected_rao: int
    fees_refunded_rao: int
    fees_retained_rao: int
    alpha_awarded_rao: int
    escrow_balance_rao: int
    entries: tuple[AccountingLedgerEntry, ...]


class BountyAccountingLedger:
    """In-memory / domain accounting engine enforcing #2046 settlement rules."""

    def __init__(self, bounty_id: UUID, reward_pool_alpha_rao: int) -> None:
        if reward_pool_alpha_rao <= 0:
            raise ValueError("reward_pool_alpha_rao must be positive")
        self.bounty_id = bounty_id
        self.reward_pool_alpha_rao = reward_pool_alpha_rao
        self._attempts: dict[UUID, AttemptRecord] = {}
        self._entries: list[AccountingLedgerEntry] = []
        self._settled_dispositions: set[tuple[UUID, str]] = set()
        self._awarded_claims: set[UUID] = set()
        self._used_extrinsic_refs: set[str] = set()

    def register_paid_attempt(
        self,
        *,
        claim_id: UUID | None = None,
        revision: int = 1,
        hotkey: str,
        payee_coldkey: str,
        github_login: str,
        entry_fee_alpha_rao: int,
        fee_extrinsic_ref: str,
    ) -> AttemptRecord:
        if entry_fee_alpha_rao <= 0:
            raise ValueError("entry_fee_alpha_rao must be positive")
        if fee_extrinsic_ref in self._used_extrinsic_refs:
            raise ValueError(f"duplicate fee extrinsic reference: {fee_extrinsic_ref}")
        cid = claim_id or uuid4()
        self._used_extrinsic_refs.add(fee_extrinsic_ref)
        attempt = AttemptRecord(
            claim_id=cid,
            bounty_id=self.bounty_id,
            revision=revision,
            hotkey=hotkey,
            payee_coldkey=payee_coldkey,
            github_login=github_login,
            entry_fee_alpha_rao=entry_fee_alpha_rao,
            reward_pool_alpha_rao=self.reward_pool_alpha_rao,
            fee_extrinsic_ref=fee_extrinsic_ref,
        )
        self._attempts[cid] = attempt
        return attempt

    def submit_attempt(
        self,
        claim_id: UUID,
        *,
        pr_number: int,
        head_sha: str,
        validation_output: str,
    ) -> AttemptRecord:
        attempt = self._get_attempt(claim_id)
        if attempt.status not in ("claimed", "rework"):
            raise ValueError(f"cannot submit from status {attempt.status}")
        if pr_number <= 0:
            raise ValueError("pr_number must be positive")
        if len(head_sha) != 40 or any(c not in "0123456789abcdefABCDEF" for c in head_sha):
            raise ValueError("head_sha must be a 40-character hex commit SHA")
        if not validation_output.strip():
            raise ValueError("validation_output must not be blank")
        attempt.pr_number = pr_number
        attempt.head_sha = head_sha.lower()
        attempt.validation_output = validation_output.strip()
        attempt.status = "submitted"
        return attempt

    def record_decision(
        self,
        claim_id: UUID,
        *,
        decision: DecisionType,
        reviewer_actor: str,
        rationale: str,
        rework_days: int | None = None,
        attribution: str | None = None,
        operator_approvals: list[str] | None = None,
        now: datetime | None = None,
    ) -> AttemptRecord:
        ts = now or datetime.now(UTC)
        attempt = self._get_attempt(claim_id)
        if not reviewer_actor.strip() or not rationale.strip():
            raise ValueError("reviewer_actor and rationale must not be blank")

        if decision == "changes_requested":
            if attempt.status != "submitted":
                raise ValueError("changes_requested requires a submitted attempt")
            if rework_days is None or not (1 <= rework_days <= 30):
                raise ValueError("rework_days between 1 and 30 is required for changes_requested")
            attempt.status = "rework"
            attempt.reviewer_actor = reviewer_actor
            attempt.reviewer_rationale = rationale
            attempt.rework_deadline_at = ts + timedelta(days=rework_days)
            attempt.decided_at = ts
            return attempt

        if decision == "accepted":
            if attempt.status != "submitted":
                raise ValueError("only a submitted attempt can be accepted")
            if not attempt.pr_number or not attempt.head_sha or not attempt.validation_output:
                raise ValueError("acceptance requires pr_number, head_sha, and passing validation_output")
            attempt.status = "accepted"
            attempt.reviewer_actor = reviewer_actor
            attempt.reviewer_rationale = rationale
            attempt.attribution = attribution or f"Accepted contribution by @{attempt.github_login}"
            attempt.decided_at = ts
            approvers = self._validate_approvers(
                operator_approvals or ["treasury-approver-1@ditto.ai"],
                excluded={reviewer_actor},
            )
            self._append_fee_disposition(
                attempt,
                reason="fee_refund",
                approvers=approvers,
                rationale=f"Entry fee refund on acceptance: {rationale}",
                now=ts,
            )
            return attempt

        if decision == "rejected":
            if attempt.status != "submitted":
                raise ValueError("only a submitted attempt can be rejected")
            attempt.status = "rejected"
            attempt.reviewer_actor = reviewer_actor
            attempt.reviewer_rationale = rationale
            attempt.decided_at = ts
            approvers = self._validate_approvers(
                operator_approvals or ["treasury-approver-1@ditto.ai"],
                excluded=set(),
            )
            self._append_fee_disposition(
                attempt,
                reason="fee_forfeit",
                approvers=approvers,
                rationale=f"Entry fee forfeited on rejection: {rationale}",
                now=ts,
            )
            return attempt

        if decision == "cancelled_by_team":
            if attempt.status in ("accepted", "rejected", "expired", "withdrawn", "cancelled_by_team"):
                raise ValueError(f"cannot team-cancel attempt in terminal status {attempt.status}")
            attempt.status = "cancelled_by_team"
            attempt.reviewer_actor = reviewer_actor
            attempt.reviewer_rationale = rationale
            attempt.decided_at = ts
            approvers = self._validate_approvers(
                operator_approvals or ["treasury-approver-1@ditto.ai"],
                excluded=set(),
            )
            self._append_fee_disposition(
                attempt,
                reason="team_cancel_refund",
                approvers=approvers,
                rationale=f"Full entry fee refund on team cancellation: {rationale}",
                now=ts,
            )
            return attempt

        raise ValueError(f"unsupported decision: {decision}")

    def mark_expired_or_withdrawn(
        self,
        claim_id: UUID,
        *,
        outcome: Literal["expired", "withdrawn"],
        rationale: str,
        operator_approvals: list[str] | None = None,
        now: datetime | None = None,
    ) -> AttemptRecord:
        ts = now or datetime.now(UTC)
        attempt = self._get_attempt(claim_id)
        if attempt.status not in ("claimed", "rework"):
            raise ValueError(f"cannot mark {outcome} from status {attempt.status}")
        attempt.status = outcome
        attempt.decided_at = ts
        approvers = self._validate_approvers(
            operator_approvals or ["treasury-approver-1@ditto.ai"],
            excluded=set(),
        )
        self._append_fee_disposition(
            attempt,
            reason="fee_forfeit",
            approvers=approvers,
            rationale=rationale,
            now=ts,
        )
        return attempt

    def award_single_winner(
        self,
        claim_id: UUID,
        *,
        amount_alpha_rao: int | None = None,
        decider_actor: str,
        operator_approvals: list[str],
        rationale: str,
        attribution: str | None = None,
        extrinsic_ref: str | None = None,
        now: datetime | None = None,
    ) -> AccountingLedgerEntry:
        award_amount = amount_alpha_rao if amount_alpha_rao is not None else self.reward_pool_alpha_rao
        entries = self.award_shared_winners(
            awards=[
                {
                    "claim_id": claim_id,
                    "amount_alpha_rao": award_amount,
                    "attribution": attribution,
                    "extrinsic_ref": extrinsic_ref,
                }
            ],
            decider_actor=decider_actor,
            operator_approvals=operator_approvals,
            rationale=rationale,
            now=now,
        )
        return entries[0]

    def award_shared_winners(
        self,
        *,
        awards: list[dict[str, object]],
        decider_actor: str,
        operator_approvals: list[str],
        rationale: str,
        now: datetime | None = None,
    ) -> list[AccountingLedgerEntry]:
        if not awards:
            raise ValueError("awards list must not be empty")
        if not rationale.strip():
            raise ValueError("award rationale must not be blank")
        ts = now or datetime.now(UTC)

        total_new = 0
        for item in awards:
            cid = item["claim_id"]
            assert isinstance(cid, UUID)
            amt = int(item["amount_alpha_rao"])  # type: ignore[arg-type]
            if amt <= 0:
                raise ValueError("award amount_alpha_rao must be positive")
            if cid in self._awarded_claims:
                raise ValueError(f"duplicate payout prevented: claim {cid} was already awarded")
            attempt = self._get_attempt(cid)
            if attempt.status != "accepted":
                raise ValueError(f"only accepted attempts can receive awards (claim {cid} is {attempt.status})")
            total_new += amt

        already_awarded = sum(
            e.amount_alpha_rao for e in self._entries if e.reason == "bounty_award"
        )
        if already_awarded + total_new > self.reward_pool_alpha_rao:
            raise ValueError(
                f"awards ({already_awarded + total_new}) would exceed reward_pool_alpha_rao ({self.reward_pool_alpha_rao})"
            )

        excluded_actors = {decider_actor}
        for item in awards:
            attempt = self._get_attempt(item["claim_id"])  # type: ignore[arg-type]
            if attempt.reviewer_actor:
                excluded_actors.add(attempt.reviewer_actor)

        approvers = self._validate_approvers(operator_approvals, excluded=excluded_actors)

        created: list[AccountingLedgerEntry] = []
        for item in awards:
            cid = item["claim_id"]
            assert isinstance(cid, UUID)
            amt = int(item["amount_alpha_rao"])  # type: ignore[arg-type]
            attempt = self._get_attempt(cid)
            ext_ref = str(
                item.get("extrinsic_ref")
                or self._deterministic_extrinsic_ref(cid, "bounty_award", amt)
            )
            if ext_ref in self._used_extrinsic_refs:
                raise ValueError(f"duplicate extrinsic reference: {ext_ref}")
            self._used_extrinsic_refs.add(ext_ref)
            self._awarded_claims.add(cid)

            entry = AccountingLedgerEntry(
                entry_id=uuid4(),
                bounty_id=self.bounty_id,
                claim_id=cid,
                recipient_coldkey=attempt.payee_coldkey,
                recipient_hotkey=attempt.hotkey,
                amount_alpha_rao=amt,
                reason="bounty_award",
                extrinsic_ref=ext_ref,
                operator_approvals=approvers,
                attribution=str(item.get("attribution") or attempt.attribution or f"Award to @{attempt.github_login}"),
                rationale=rationale,
                recorded_at=ts,
            )
            self._entries.append(entry)
            created.append(entry)

        return created

    def summary(self) -> BountyAccountingSummary:
        total_collected = sum(a.entry_fee_alpha_rao for a in self._attempts.values())
        refunded = sum(
            e.amount_alpha_rao
            for e in self._entries
            if e.reason in ("fee_refund", "team_cancel_refund")
        )
        retained = sum(
            e.amount_alpha_rao for e in self._entries if e.reason == "fee_forfeit"
        )
        awarded = sum(
            e.amount_alpha_rao for e in self._entries if e.reason == "bounty_award"
        )
        escrow_balance = total_collected - refunded
        return BountyAccountingSummary(
            bounty_id=self.bounty_id,
            reward_pool_alpha_rao=self.reward_pool_alpha_rao,
            total_fees_collected_rao=total_collected,
            fees_refunded_rao=refunded,
            fees_retained_rao=retained,
            alpha_awarded_rao=awarded,
            escrow_balance_rao=escrow_balance,
            entries=tuple(self._entries),
        )

    def _get_attempt(self, claim_id: UUID) -> AttemptRecord:
        if claim_id not in self._attempts:
            raise KeyError(f"unknown claim_id: {claim_id}")
        return self._attempts[claim_id]

    def _validate_approvers(
        self, approvers: list[str], *, excluded: set[str]
    ) -> tuple[str, ...]:
        cleaned = [a.strip() for a in approvers if a.strip()]
        if not cleaned:
            raise ValueError("at least one operator approval is required")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("operator approvals must be distinct")
        for actor in cleaned:
            if actor in excluded:
                raise PermissionError(
                    f"operator '{actor}' decided the review or award and cannot approve its payout"
                )
        return tuple(cleaned)

    def _append_fee_disposition(
        self,
        attempt: AttemptRecord,
        *,
        reason: AccountingReason,
        approvers: tuple[str, ...],
        rationale: str,
        now: datetime,
    ) -> AccountingLedgerEntry:
        disp_key = (attempt.claim_id, "fee_disposition")
        if disp_key in self._settled_dispositions:
            raise ValueError(f"fee disposition already recorded for claim {attempt.claim_id}")
        self._settled_dispositions.add(disp_key)

        ext_ref = self._deterministic_extrinsic_ref(
            attempt.claim_id, reason, attempt.entry_fee_alpha_rao
        )
        self._used_extrinsic_refs.add(ext_ref)
        entry = AccountingLedgerEntry(
            entry_id=uuid4(),
            bounty_id=self.bounty_id,
            claim_id=attempt.claim_id,
            recipient_coldkey=attempt.payee_coldkey,
            recipient_hotkey=attempt.hotkey,
            amount_alpha_rao=attempt.entry_fee_alpha_rao,
            reason=reason,
            extrinsic_ref=ext_ref,
            operator_approvals=approvers,
            attribution=attempt.attribution,
            rationale=rationale,
            recorded_at=now,
        )
        self._entries.append(entry)
        return entry

    @staticmethod
    def _deterministic_extrinsic_ref(claim_id: UUID, reason: str, amount: int) -> str:
        digest = hashlib.sha256(f"{claim_id}:{reason}:{amount}".encode("utf-8")).hexdigest()
        return f"0x{digest}#0"
