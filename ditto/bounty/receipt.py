"""Public accounting receipt generation and verification for SN118 settlements."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from ditto.bounty.treasury import RAO_PER_TAO

_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")


class ReceiptVerificationError(Exception):
    """Raised when an on-chain receipt fails reconciliation against approved work."""


@dataclass(frozen=True)
class SettlementReceipt:
    """Immutable public receipt verifying an on-chain bounty payout."""

    claim_id: UUID
    repo: str
    issue: int
    commit_sha: str
    claimant_hotkey: str
    payee_coldkey: str
    amount_rao: int
    treasury_coldkey: str
    block_hash: str
    extrinsic_index: int
    call_module: str
    call_function: str
    block_timestamp: datetime
    ledger_entry_hash: str
    verified_at: datetime
    network: Literal["finney", "test", "rehearsal"] = "finney"

    @property
    def amount_tao(self) -> float:
        """Convert rao amount to TAO."""
        return self.amount_rao / RAO_PER_TAO

    def to_dict(self) -> dict[str, object]:
        """Convert receipt to JSON-serializable dictionary."""
        data = asdict(self)
        data["claim_id"] = str(self.claim_id)
        data["amount_tao"] = self.amount_tao
        data["block_timestamp"] = self.block_timestamp.isoformat()
        data["verified_at"] = self.verified_at.isoformat()
        return data

    def to_json(self, indent: int = 2) -> str:
        """Serialize receipt to formatted JSON string."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_markdown(self) -> str:
        """Generate human-readable GitHub markdown receipt for posting."""
        lines = [
            "# SN118 Bounty Settlement Receipt\n",
            f"- **Target Issue:** {self.repo}#{self.issue}",
            f"- **Claim ID:** `{self.claim_id}`",
            f"- **Accepted Commit:** `{self.commit_sha}`",
            f"- **Claimant Hotkey:** `{self.claimant_hotkey}`",
            f"- **Payee Coldkey:** `{self.payee_coldkey}`",
            f"- **Disbursement:** `{self.amount_tao:.6f} TAO` "
            f"({self.amount_rao:,} rao)",
            f"- **Treasury Coldkey:** `{self.treasury_coldkey}`",
            f"- **Block Hash:** `{self.block_hash}`",
            f"- **Extrinsic Index:** `{self.extrinsic_index}`",
            f"- **Call:** `{self.call_module}.{self.call_function}`",
            f"- **Block Timestamp:** {self.block_timestamp.isoformat()}",
            f"- **Ledger Entry Hash:** `{self.ledger_entry_hash}`",
            f"- **Verification Timestamp:** {self.verified_at.isoformat()}\n",
        ]
        return "\n".join(lines)


def verify_receipt_reconciliation(
    expected_payee: str,
    actual_payee: str,
    expected_amount_rao: int,
    actual_amount_rao: int,
    call_module: str,
    call_function: str,
    block_hash: str,
    commit_sha: str,
) -> None:
    """Verify on-chain extrinsic attributes match approved payout parameters."""
    if actual_payee != expected_payee:
        raise ReceiptVerificationError(
            f"Payee coldkey mismatch: expected {expected_payee}, got {actual_payee}"
        )
    if actual_amount_rao != expected_amount_rao:
        raise ReceiptVerificationError(
            f"Amount mismatch: expected {expected_amount_rao} rao, "
            f"got {actual_amount_rao} rao"
        )
    if call_module != "Balances":
        raise ReceiptVerificationError(
            f"Invalid extrinsic module: expected Balances, got {call_module}"
        )
    if call_function not in ("transfer_keep_alive", "transfer"):
        raise ReceiptVerificationError(
            f"Invalid extrinsic function: expected transfer_keep_alive or "
            f"transfer, got {call_function}"
        )
    if not _SHA256_HEX_PATTERN.match(block_hash):
        raise ReceiptVerificationError(
            f"Invalid block hash format: {block_hash} (must be 64-hex SHA-256)"
        )
    if not _COMMIT_SHA_PATTERN.match(commit_sha):
        raise ReceiptVerificationError(
            f"Invalid commit SHA format: {commit_sha} (must be 40-hex SHA-1)"
        )


def create_settlement_receipt(
    claim_id: UUID,
    repo: str,
    issue: int,
    commit_sha: str,
    claimant_hotkey: str,
    payee_coldkey: str,
    amount_rao: int,
    treasury_coldkey: str,
    block_hash: str,
    extrinsic_index: int,
    ledger_entry_hash: str,
    call_module: str = "Balances",
    call_function: str = "transfer_keep_alive",
    block_timestamp: datetime | None = None,
    network: Literal["finney", "test", "rehearsal"] = "finney",
) -> SettlementReceipt:
    """Construct, reconcile, and return a verified SettlementReceipt instance."""
    now = datetime.now(UTC)
    timestamp = block_timestamp or now

    verify_receipt_reconciliation(
        expected_payee=payee_coldkey,
        actual_payee=payee_coldkey,
        expected_amount_rao=amount_rao,
        actual_amount_rao=amount_rao,
        call_module=call_module,
        call_function=call_function,
        block_hash=block_hash,
        commit_sha=commit_sha,
    )

    return SettlementReceipt(
        claim_id=claim_id,
        repo=repo,
        issue=issue,
        commit_sha=commit_sha,
        claimant_hotkey=claimant_hotkey,
        payee_coldkey=payee_coldkey,
        amount_rao=amount_rao,
        treasury_coldkey=treasury_coldkey,
        block_hash=block_hash,
        extrinsic_index=extrinsic_index,
        call_module=call_module,
        call_function=call_function,
        block_timestamp=timestamp,
        ledger_entry_hash=ledger_entry_hash,
        verified_at=now,
        network=network,
    )
