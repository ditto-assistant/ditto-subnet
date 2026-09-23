"""Bounty acceptance, approvals, payout verification, and public accounting."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from ditto.api_models.bounty_payout import (
    BountyApprovalRecord,
    BountyLedgerEntry,
    BountyPayoutProof,
    BountyPayoutReceipt,
    BountySplitShare,
    OperatorApproval,
    PayoutState,
    PublicBountyAuditRecord,
)

GENESIS_HASH = "0" * 64
CONFIRMATION_PHRASE = "APPROVE BOUNTY PAYOUT"

TIER_1_MAX_RAO = 5_000_000_000
TIER_2_MAX_RAO = 25_000_000_000
ENDORSEMENT_THRESHOLD = 3

EXPECTED_CALL_MODULE = "Balances"
EXPECTED_CALL_FUNCTIONS = ("transfer_keep_alive", "transfer")

VALID_TRANSITIONS: dict[PayoutState, set[PayoutState]] = {
    "claimed": {"submitted", "cancelled", "handed_off"},
    "submitted": {"accepted", "rejected", "cancelled"},
    "accepted": {"merged", "rejected", "cancelled"},
    "merged": {"deployed_verified", "rejected", "cancelled"},
    "deployed_verified": {"approved", "rejected", "cancelled"},
    "approved": {"paid", "payout_failed", "cancelled"},
    "paid": set(),
    "rejected": {"appealed", "cancelled"},
    "appealed": {"accepted", "rejected", "cancelled"},
    "cancelled": set(),
    "handed_off": set(),
    "payout_failed": {"paid", "cancelled"},
}


class BountyPayoutError(Exception):
    """Base exception for all bounty acceptance and payout errors."""


class InvalidStateTransitionError(BountyPayoutError):
    """Raised when an illegal lifecycle state transition is attempted."""


class MergeCannotTriggerPaymentError(BountyPayoutError):
    """Raised when attempting to disburse payment directly from merged state."""


class InsufficientApprovalsError(BountyPayoutError):
    """Raised when an approval record lacks the required multi-operator threshold."""


class DuplicateApproverError(BountyPayoutError):
    """Raised when multiple approvals originate from the same operator."""


class DuplicatePaymentError(BountyPayoutError):
    """Raised when an extrinsic or deliverable commit SHA has already been paid."""


class InvalidPayoutProofError(BountyPayoutError):
    """Raised when on-chain extrinsic verification fails."""


class InvalidSplitSharesError(BountyPayoutError):
    """Raised when split shares do not strictly conserve total approved amount."""


class ApprovalChecksumMismatchError(BountyPayoutError):
    """Raised when an approval record does not match its cryptographic checksum."""


class DisputeError(BountyPayoutError):
    """Raised when an invalid dispute or appeal action is attempted."""


def canonical_json_bytes(content: dict[str, Any]) -> bytes:
    """Encode dictionary into deterministic, canonical JSON bytes."""
    return json.dumps(
        content,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def compute_entry_hash(content: dict[str, Any]) -> str:
    """Compute SHA-256 digest over canonical JSON content."""
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def _extract_val(obj: Any, key: str) -> Any:
    return obj[key] if isinstance(obj, dict) else getattr(obj, key)


def _normalize_iso(val: Any) -> str:
    if isinstance(val, datetime):
        val = val.isoformat()
    return str(val).replace("+00:00", "Z")


def compute_approval_checksum(record_dict: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 checksum over approval payload fields."""
    raw_shares = record_dict.get("split_shares", [])
    shares_list = [
        {
            "claimant_hotkey": _extract_val(s, "claimant_hotkey"),
            "payee_coldkey": _extract_val(s, "payee_coldkey"),
            "amount_rao": _extract_val(s, "amount_rao"),
            "share_ratio": _extract_val(s, "share_ratio"),
        }
        for s in raw_shares
    ]
    raw_approvals = record_dict.get("approvals", [])
    approvals_list = []
    for a in raw_approvals:
        app_at = _extract_val(a, "approved_at")
        approvals_list.append(
            {
                "operator_hotkey": _extract_val(a, "operator_hotkey"),
                "signature": _extract_val(a, "signature"),
                "role": _extract_val(a, "role"),
                "approved_at": _normalize_iso(app_at),
            }
        )

    payload = {
        "repo": record_dict["repo"],
        "issue": record_dict["issue"],
        "claim_id": str(record_dict["claim_id"]),
        "commit_sha": record_dict["commit_sha"],
        "claimant_hotkey": record_dict["claimant_hotkey"],
        "payee_coldkey": record_dict["payee_coldkey"],
        "reward_revision": record_dict["reward_revision"],
        "amount_rao": record_dict["amount_rao"],
        "amount_tao": record_dict["amount_tao"],
        "treasury_policy_revision": record_dict["treasury_policy_revision"],
        "split_shares": shares_list,
        "approvals": approvals_list,
        "confirmation_phrase": record_dict.get(
            "confirmation_phrase", CONFIRMATION_PHRASE
        ),
    }
    return compute_entry_hash(payload)


def required_approvals_for_amount(amount_rao: int) -> int:
    """Determine required distinct operator approval count for given amount."""
    if amount_rao <= TIER_1_MAX_RAO:
        return 1
    if amount_rao <= TIER_2_MAX_RAO:
        return 2
    return ENDORSEMENT_THRESHOLD


def validate_operator_approvals(
    amount_rao: int,
    approvals: list[OperatorApproval],
    authorized_operators: set[str] | None = None,
) -> None:
    """Validate operator approval count and distinctness for amount tier."""
    required_count = required_approvals_for_amount(amount_rao)
    unique_operators = {a.operator_hotkey for a in approvals}
    if len(unique_operators) != len(approvals):
        raise DuplicateApproverError("Duplicate operator approvals are forbidden")
    if len(approvals) < required_count:
        raise InsufficientApprovalsError(
            f"Amount {amount_rao} rao requires at least {required_count} "
            f"distinct approvals, but only {len(approvals)} provided"
        )
    if authorized_operators is not None:
        unauthorized = unique_operators - authorized_operators
        if unauthorized:
            raise InsufficientApprovalsError(
                f"Approvals contain unauthorized operator keys: {sorted(unauthorized)}"
            )


def validate_split_shares(
    amount_rao: int, split_shares: list[BountySplitShare]
) -> None:
    """Ensure split shares strictly sum to the total approved amount in rao."""
    if not split_shares:
        return
    total_allocated = sum(s.amount_rao for s in split_shares)
    if total_allocated != amount_rao:
        raise InvalidSplitSharesError(
            f"Sum of split shares ({total_allocated} rao) does not equal "
            f"approved amount ({amount_rao} rao)"
        )
    for share in split_shares:
        if share.amount_rao <= 0:
            raise InvalidSplitSharesError("Split share amount must be positive")


def verify_bounty_ledger_chain(
    entries: list[BountyLedgerEntry],
    expected_prev: str = GENESIS_HASH,
) -> bool:
    """Verify cryptographic hash chaining and integrity across ledger entries."""
    running = expected_prev
    for entry in entries:
        if entry.prev_hash != running:
            return False
        content = {
            "seq": entry.seq,
            "entry_id": str(entry.entry_id),
            "claim_id": str(entry.claim_id),
            "repo": entry.repo,
            "issue": entry.issue,
            "event": entry.event,
            "payload": entry.payload,
            "prev_hash": entry.prev_hash,
            "recorded_at": entry.recorded_at.isoformat(),
        }
        if compute_entry_hash(content) != entry.entry_hash:
            return False
        running = entry.entry_hash
    return True


class BountyStateMachine:
    """Validates bounty lifecycle transitions and prevents unauthorized payouts."""

    def __init__(self, current_state: PayoutState = "claimed") -> None:
        self._current_state = current_state
        self._history: list[dict[str, Any]] = [
            {"state": current_state, "timestamp": datetime.now(UTC).isoformat()}
        ]

    @property
    def current_state(self) -> PayoutState:
        """Current lifecycle state."""
        return self._current_state

    @property
    def history(self) -> list[dict[str, Any]]:
        """Ordered history of lifecycle state transitions."""
        return list(self._history)

    def transition_to(
        self,
        new_state: PayoutState,
        *,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        """Transition to a new lifecycle state enforcing invariant checks."""
        if self._current_state == "merged" and new_state == "paid":
            raise MergeCannotTriggerPaymentError(
                "Merge alone cannot trigger payment; deployment verification "
                "and multi-operator approval are strictly required"
            )

        allowed = VALID_TRANSITIONS.get(self._current_state, set())
        if new_state not in allowed:
            raise InvalidStateTransitionError(
                f"Illegal transition from {self._current_state!r} to {new_state!r}"
            )

        ts = now or datetime.now(UTC)
        self._current_state = new_state
        record: dict[str, Any] = {"state": new_state, "timestamp": ts.isoformat()}
        if metadata:
            record["metadata"] = metadata
        self._history.append(record)


class BountyLedger:
    """Append-only, tamper-evident hash-chained public accounting ledger."""

    def __init__(self) -> None:
        self._entries: list[BountyLedgerEntry] = []
        self._current_seq = 0

    @property
    def head_hash(self) -> str:
        """Current chain head hash or genesis hash if empty."""
        return self._entries[-1].entry_hash if self._entries else GENESIS_HASH

    @property
    def entries(self) -> list[BountyLedgerEntry]:
        """All recorded ledger entries in chronological order."""
        return list(self._entries)

    def append(
        self,
        *,
        claim_id: UUID,
        repo: str,
        issue: int,
        event: str,
        payload: dict[str, Any],
        now: datetime | None = None,
    ) -> BountyLedgerEntry:
        """Append a new hash-linked entry to the ledger."""
        self._current_seq += 1
        entry_id = uuid4()
        ts = now or datetime.now(UTC)
        prev_hash = self.head_hash

        content = {
            "seq": self._current_seq,
            "entry_id": str(entry_id),
            "claim_id": str(claim_id),
            "repo": repo,
            "issue": issue,
            "event": event,
            "payload": payload,
            "prev_hash": prev_hash,
            "recorded_at": ts.isoformat(),
        }
        entry_hash = compute_entry_hash(content)

        entry = BountyLedgerEntry(
            seq=self._current_seq,
            entry_id=entry_id,
            claim_id=claim_id,
            repo=repo,
            issue=issue,
            event=event,
            payload=payload,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
            recorded_at=ts,
        )
        self._entries.append(entry)
        return entry

    def verify(self) -> bool:
        """Verify complete ledger chain integrity from genesis to head."""
        return verify_bounty_ledger_chain(self._entries)


class BountyChainPayoutVerifier:
    """Verifies on-chain Substrate extrinsics submitted as payout proofs."""

    def __init__(
        self,
        authorized_treasury_coldkeys: set[str],
    ) -> None:
        self._authorized_treasury_coldkeys = authorized_treasury_coldkeys
        self._settled_receipts: set[tuple[str, int]] = set()
        self._paid_commits: set[tuple[str, int, str]] = set()

    async def verify_payout(
        self,
        *,
        chain_client: Any,
        proof: BountyPayoutProof,
        approval: BountyApprovalRecord,
        now: datetime | None = None,
    ) -> BountyPayoutReceipt:
        """Verify on-chain transaction parameters and enforce double-spend rules."""
        if approval.confirmation_phrase != CONFIRMATION_PHRASE:
            raise ApprovalChecksumMismatchError(
                f"Invalid confirmation phrase: expected {CONFIRMATION_PHRASE!r}"
            )

        approval_dict = approval.model_dump(mode="json")
        expected_checksum = compute_approval_checksum(approval_dict)
        if approval.checksum != expected_checksum:
            raise ApprovalChecksumMismatchError(
                f"Approval checksum mismatch: expected {expected_checksum}, "
                f"got {approval.checksum}"
            )

        validate_operator_approvals(approval.amount_rao, approval.approvals)

        if approval.split_shares:
            validate_split_shares(approval.amount_rao, approval.split_shares)

        work_key = (approval.repo, approval.issue, approval.commit_sha)
        if work_key in self._paid_commits:
            raise DuplicatePaymentError(
                f"Accepted work {approval.repo}#{approval.issue} at commit "
                f"{approval.commit_sha} has already been paid"
            )

        canonical_block_hash = (
            await chain_client.get_block_hash(proof.block_number)
        ).lower()
        if canonical_block_hash != proof.block_hash.lower():
            raise InvalidPayoutProofError(
                f"Block number {proof.block_number} resolves to "
                f"{canonical_block_hash}, not {proof.block_hash}"
            )

        extrinsic_key = (canonical_block_hash, proof.extrinsic_index)
        if extrinsic_key in self._settled_receipts:
            raise DuplicatePaymentError(
                f"Extrinsic at block {canonical_block_hash} index "
                f"{proof.extrinsic_index} has already been recorded"
            )

        ext = await chain_client.get_extrinsic(
            proof.block_number, proof.extrinsic_index
        )

        if (
            ext.call_module != EXPECTED_CALL_MODULE
            or ext.call_function not in EXPECTED_CALL_FUNCTIONS
        ):
            raise InvalidPayoutProofError(
                f"Expected call in {EXPECTED_CALL_FUNCTIONS}, got "
                f"{ext.call_module}.{ext.call_function}"
            )

        succeeded = await chain_client.check_extrinsic_success(
            canonical_block_hash, proof.extrinsic_index
        )
        if not succeeded:
            raise InvalidPayoutProofError(
                f"Extrinsic at block {canonical_block_hash} index "
                f"{proof.extrinsic_index} emitted ExtrinsicFailed"
            )

        dest_coldkey = ext.call_args.get("dest")
        if dest_coldkey != proof.payee_coldkey:
            raise InvalidPayoutProofError(
                f"Destination {dest_coldkey!r} does not match expected payee "
                f"{proof.payee_coldkey!r}"
            )

        paid_value = int(ext.call_args.get("value", 0))
        if paid_value != proof.amount_rao:
            raise InvalidPayoutProofError(
                f"Paid {paid_value} rao does not match expected {proof.amount_rao} rao"
            )

        signer = ext.signer_address
        if signer not in self._authorized_treasury_coldkeys:
            raise InvalidPayoutProofError(
                f"Extrinsic signer {signer!r} is not an authorized treasury coldkey"
            )

        block_ts_seconds = await chain_client.get_block_timestamp(canonical_block_hash)
        block_ts = datetime.fromtimestamp(block_ts_seconds, tz=UTC)
        verified_ts = now or datetime.now(UTC)

        self._settled_receipts.add(extrinsic_key)
        self._paid_commits.add(work_key)

        return BountyPayoutReceipt(
            block_hash=canonical_block_hash,
            extrinsic_index=proof.extrinsic_index,
            claim_id=proof.claim_id,
            payee_coldkey=proof.payee_coldkey,
            amount_rao=paid_value,
            treasury_coldkey=signer,
            block_timestamp=block_ts,
            verified_at=verified_ts,
        )


def export_public_audit_record(
    *,
    approval: BountyApprovalRecord,
    state_machine: BountyStateMachine,
    receipt: BountyPayoutReceipt | None = None,
    now: datetime | None = None,
) -> PublicBountyAuditRecord:
    """Project a sanitized public audit view redacting private notes."""
    ts = now or datetime.now(UTC)
    return PublicBountyAuditRecord(
        repo=approval.repo,
        issue=approval.issue,
        claim_id=approval.claim_id,
        status=state_machine.current_state,
        commit_sha=approval.commit_sha,
        claimant_hotkey=approval.claimant_hotkey,
        payee_coldkey=approval.payee_coldkey,
        amount_rao=approval.amount_rao,
        amount_tao=approval.amount_tao,
        block_hash=receipt.block_hash if receipt else None,
        extrinsic_index=receipt.extrinsic_index if receipt else None,
        state_transitions=state_machine.history,
        updated_at=ts,
    )
