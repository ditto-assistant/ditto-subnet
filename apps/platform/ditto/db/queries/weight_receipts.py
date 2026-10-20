"""Immutable authenticated commit claims, checked against the frozen ledger."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.weight_receipt import (
    FinalizedWeightReceipt,
    SubmitWeightReceiptRequest,
    weight_receipt_digest,
)
from ditto.db.models import (
    LedgerEpochSnapshot,
    ValidatorWeightReceipt,
    ValidatorWeightRequest,
)
from ditto_screening_protocol.treasury_approval import (
    verify_policy_approval,
    verify_public_signature,
)
from ditto_screening_protocol.treasury_enforcement import EnforcingTreasuryPin

WeightReceiptConflictCode = Literal[
    "unknown_ledger_snapshot",
    "ledger_pin_mismatch",
    "artifact_pin_mismatch",
    "commit_before_pin",
    "request_rebound",
    "attempt_rebound",
]


class WeightReceiptConflict(ValueError):
    """A job/attempt identity was rebound or its provenance is not the frozen pin.

    ``code`` is a closed category safe for responses and logs; neither it nor the
    message ever carries receipt contents. ``claimed_provenance`` carries identity
    ids only (snapshot, epoch, champion) so a poisoned envelope can be triaged
    without exposing receipt contents.
    """

    def __init__(
        self,
        code: WeightReceiptConflictCode,
        message: str,
        *,
        claimed_provenance: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.claimed_provenance = claimed_provenance


def _claimed_provenance(receipt: FinalizedWeightReceipt) -> str:
    """Identity ids of the receipt's claim; never receipt contents."""
    provenance = receipt.provenance
    champion = (
        str(provenance.champion_agent_id)
        if provenance.champion_agent_id is not None
        else "none"
    )
    return (
        f"claimed snapshot={provenance.ledger_snapshot_id}"
        f" epoch={provenance.epoch_index}"
        f" digest={provenance.ledger_digest[:12]}"
        f" bench={provenance.bench_version}"
        f" champion={champion}"
        f" artifact={str(provenance.champion_artifact_sha256)[:12]}"
    )


async def _validate_provenance(
    session: AsyncSession, receipt: FinalizedWeightReceipt
) -> None:
    provenance = receipt.provenance
    pin = await session.get(LedgerEpochSnapshot, provenance.ledger_snapshot_id)
    if pin is None:
        raise WeightReceiptConflict(
            "unknown_ledger_snapshot",
            "unknown immutable ledger snapshot",
            claimed_provenance=_claimed_provenance(receipt),
        )
    digest = hashlib.sha256(
        json.dumps(
            {"entries": pin.entries, "served": pin.context.get("served", {})},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    if (
        pin.netuid != receipt.netuid
        or pin.epoch_index != provenance.epoch_index
        or pin.ledger_digest != provenance.ledger_digest
        or digest != pin.ledger_digest
        or pin.bench_version != provenance.bench_version
        or pin.champion_agent_id != provenance.champion_agent_id
    ):
        raise WeightReceiptConflict(
            "ledger_pin_mismatch",
            "receipt does not match its immutable champion pin",
            claimed_provenance=_claimed_provenance(receipt),
        )
    served = pin.context.get("served", {})
    raw_treasury = served.get("treasury_pin")
    if receipt.schema_version == 2:
        try:
            treasury = EnforcingTreasuryPin.model_validate(raw_treasury)
            if treasury != receipt.treasury_pin or (
                treasury.epoch_index != pin.epoch_index
                or treasury.first_block != pin.last_epoch_block
                or treasury.pinned_block != pin.pinned_block
                or treasury.pinned_block_hash != pin.pinned_block_hash
            ):
                raise ValueError("treasury receipt is not the frozen epoch contract")
            verify_policy_approval(
                treasury.approval,
                expected_policy_digest=treasury.policy_digest,
                expected_collector_policy_digest=treasury.policy.collector_policy_digest,
                verify_signature=verify_public_signature,
            )
        except (ValueError, TypeError):
            raise WeightReceiptConflict(
                "ledger_pin_mismatch",
                "invalid treasury epoch provenance",
                claimed_provenance=_claimed_provenance(receipt),
            ) from None
    elif isinstance(raw_treasury, dict) and raw_treasury.get("version") == 2:
        raise WeightReceiptConflict(
            "ledger_pin_mismatch",
            "enforcing epoch requires V2 receipt",
            claimed_provenance=_claimed_provenance(receipt),
        )
    # A provisional incumbent (protocol 28) holds the pin's crown from its served
    # markers rather than from the payable entries; it is folded, never paid.
    provisional = (
        [served["provisional_incumbent"]]
        if served.get("crown_mode") == "incumbent"
        and served.get("provisional_incumbent")
        else []
    )
    entries = [
        entry
        for entry in (*pin.entries, *provisional)
        if str(entry.get("agent_id")) == str(provenance.champion_agent_id)
    ]
    if provenance.champion_agent_id is None and receipt.schema_version == 2:
        entries = []
    elif (
        len(entries) != 1
        or entries[0].get("sha256") != provenance.champion_artifact_sha256
    ):
        raise WeightReceiptConflict(
            "artifact_pin_mismatch",
            "receipt artifact does not match its immutable pin",
            claimed_provenance=_claimed_provenance(receipt),
        )
    if pin.pinned_block >= receipt.attempt.commit_block:
        raise WeightReceiptConflict(
            "commit_before_pin",
            "receipt commit does not follow its ledger pin",
            claimed_provenance=_claimed_provenance(receipt),
        )


async def record_weight_receipt(
    session: AsyncSession,
    *,
    submission: SubmitWeightReceiptRequest,
    now: datetime,
) -> str:
    """Write atomically in caller transaction; retries retain the first signed claim.

    This validates identity, not chain finality or payout. Only the independent
    chain verifier may consume the claim for source disclosure.
    """
    receipt = submission.receipt
    digest = weight_receipt_digest(receipt)
    key = (receipt.validator_hotkey, receipt.request_id, receipt.attempt.attempt_id)
    stored = await session.get(ValidatorWeightReceipt, key)
    if stored is not None:
        # The digest covers every field of the (already signature-verified)
        # body, so equality means this exact claim passed every check below
        # when first stored; its pin and request binding are immutable.
        if stored.receipt_digest != digest:
            raise WeightReceiptConflict(
                "attempt_rebound",
                "Pylon attempt identity already has a different receipt",
            )
        return digest
    await _validate_provenance(session, receipt)
    request = receipt.model_dump(mode="json", exclude={"attempt"})
    await session.execute(
        insert(ValidatorWeightRequest)
        .values(
            validator_hotkey=receipt.validator_hotkey,
            request_id=receipt.request_id,
            netuid=receipt.netuid,
            request_digest=receipt.request_digest,
            request=request,
            first_seen_at=now,
        )
        .on_conflict_do_nothing(index_elements=["validator_hotkey", "request_id"])
    )
    bound = await session.get(
        ValidatorWeightRequest, (receipt.validator_hotkey, receipt.request_id)
    )
    if bound is None or bound.request != request:
        raise WeightReceiptConflict(
            "request_rebound", "Pylon request identity already has different provenance"
        )
    await session.execute(
        insert(ValidatorWeightReceipt)
        .values(
            validator_hotkey=receipt.validator_hotkey,
            request_id=receipt.request_id,
            attempt_id=receipt.attempt.attempt_id,
            netuid=receipt.netuid,
            receipt_digest=digest,
            ciphertext_hash=receipt.attempt.ciphertext_hash,
            receipt=receipt.model_dump(mode="json"),
            first_seen_at=now,
            signed_at=submission.timestamp,
            signature=submission.signature,
        )
        .on_conflict_do_nothing(
            index_elements=["validator_hotkey", "request_id", "attempt_id"]
        )
    )
    stored = await session.get(ValidatorWeightReceipt, key)
    if stored is None or stored.receipt_digest != digest:
        raise WeightReceiptConflict(
            "attempt_rebound", "Pylon attempt identity already has a different receipt"
        )
    return digest


async def get_finalized_weight_receipts(
    session: AsyncSession,
    *,
    netuid: int,
    validator_hotkey: str,
    ciphertext_hash: str,
) -> list[ValidatorWeightReceipt]:
    """Return claims, not trusted proofs; ambiguity must fail closed in the verifier."""
    return list(
        await session.scalars(
            select(ValidatorWeightReceipt).where(
                ValidatorWeightReceipt.netuid == netuid,
                ValidatorWeightReceipt.validator_hotkey == validator_hotkey,
                ValidatorWeightReceipt.ciphertext_hash == ciphertext_hash,
            )
        )
    )


async def get_weight_receipt_by_digest(
    session: AsyncSession,
    *,
    receipt_digest: str,
) -> ValidatorWeightReceipt | None:
    return await session.scalar(
        select(ValidatorWeightReceipt).where(
            ValidatorWeightReceipt.receipt_digest == receipt_digest,
        )
    )
