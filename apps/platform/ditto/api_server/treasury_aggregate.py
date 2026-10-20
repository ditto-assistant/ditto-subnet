"""Independent multi-epoch source proof for one finalized alpha transfer."""

from dataclasses import asdict, replace


def parent_source_identity(proof, block):
    from ditto.api_server.treasury_ingress import ReceiptConflict

    if not isinstance(proof, dict):
        raise ReceiptConflict("parent source proof is invalid")
    source = proof
    claims = proof.get("source_claims")
    if claims:
        if not isinstance(claims, list):
            raise ReceiptConflict("parent aggregate source is invalid")
        matches = [
            c for c in claims if isinstance(c, dict) and c.get("source_block") == block
        ]
        if len(matches) != 1:
            raise ReceiptConflict("parent aggregate source is ambiguous")
        source = matches[0]
    try:
        return tuple(
            source[k]
            for k in ("source_block_hash", "source_event_digest", "source_amount_rao")
        )
    except KeyError as error:
        raise ReceiptConflict("parent source identity is incomplete") from error


def source_consumption(
    receipts,
    block,
    policy_digest,
    settings_revision,
    proof,
    *,
    ignore_receipt=None,
):
    from ditto.api_server.treasury_ingress import ReceiptConflict

    total = 0
    identity = (
        proof.source_block_hash,
        proof.source_event_digest,
        proof.source_amount_rao,
    )
    for receipt in receipts:
        if receipt.receipt_id == ignore_receipt:
            continue
        claims = receipt.proof.get("source_claims", [])
        if claims:
            matches = [c for c in claims if c["source_block"] == block]
        elif receipt.source_block == block:
            matches = [
                {
                    "amount_rao": receipt.amount_atomic,
                    "policy_digest": receipt.policy_digest,
                    "settings_revision": receipt.settings_revision,
                    **{
                        k: receipt.proof[k]
                        for k in (
                            "source_block_hash",
                            "source_event_digest",
                            "source_amount_rao",
                        )
                    },
                }
            ]
        else:
            matches = []
        if len(matches) > 1:
            raise ReceiptConflict("duplicate retained source attribution")
        for claim in matches:
            if (
                claim["policy_digest"] != policy_digest
                or claim["settings_revision"] != settings_revision
                or tuple(
                    claim[k]
                    for k in (
                        "source_block_hash",
                        "source_event_digest",
                        "source_amount_rao",
                    )
                )
                != identity
            ):
                raise ReceiptConflict(
                    "source credit differs from retained distribution"
                )
            total += claim["amount_rao"]
    return total


async def aggregate_receipt(session, chain, selector, *, record):
    from ditto.api_models.treasury_ingress import TreasuryReceiptPreflight
    from ditto.api_server.treasury_ingress import (
        ReceiptConflict,
        _receipt,
        digest,
        receipt_result,
        save_receipt,
    )
    from ditto.db.models import TreasuryVerifiedReceipt

    evidence = {}
    # Acquire all immutable epoch row locks in stable order. One transaction
    # verifies and consumes every source; no partial receipt can be published.
    for claim in sorted(
        selector.source_claims, key=lambda c: (c.epoch_index, c.source_block)
    ):
        single = selector.model_copy(
            update={
                "source_claims": [],
                "source_block": claim.source_block,
                "epoch_index": claim.epoch_index,
            }
        )
        checked = await _receipt(
            session,
            chain,
            single,
            record=record,
            claim_amount=claim.amount_rao,
            evidence_only=True,
        )
        if not isinstance(checked, dict):
            raise ValueError("source verification did not return evidence")
        evidence[claim.source_block] = checked
    first = evidence[selector.source_block]
    proof, policy, bucket = first["proof"], first["policy"], first["bucket"]
    claims = []
    for claim in selector.source_claims:
        current = evidence[claim.source_block]
        source = current["proof"]
        if (
            current["receipt_id"] != first["receipt_id"]
            or (source.sender, source.recipient, source.asset, source.amount_atomic)
            != (proof.sender, proof.recipient, proof.asset, proof.amount_atomic)
            or (current["policy"].collector_coldkey, current["policy"].collector_hotkey)
            != (policy.collector_coldkey, policy.collector_hotkey)
            or current["bucket"].holding_coldkey != bucket.holding_coldkey
            or current["bucket"].publish_payments != bucket.publish_payments
        ):
            raise ReceiptConflict("aggregate sources differ in payment or destination")
        claims.append(
            {
                **claim.model_dump(),
                "policy_digest": current["policy"].digest,
                "collector_policy_digest": current["policy"].collector_policy_digest,
                "settings_revision": current["revision"].revision,
                **{
                    k: asdict(source)[k]
                    for k in (
                        "source_block_hash",
                        "source_event_digest",
                        "source_amount_rao",
                    )
                },
            }
        )
    public = {**first["public_proof"], "source_claims": claims}
    public["source_amount_rao"] = sum(c["source_amount_rao"] for c in claims)
    request_digest = digest(
        {
            "selector": selector.model_dump(mode="json", exclude={"reason"}),
            "proof": public,
        }
    )
    existing = await session.get(TreasuryVerifiedReceipt, first["receipt_id"])
    if existing:
        if existing.request_digest != request_digest:
            raise ReceiptConflict(
                "chain effect already has a different source allocation"
            )
        return (
            receipt_result(existing, replayed=True)
            if record
            else TreasuryReceiptPreflight(
                ready=True,
                receipt_id=first["receipt_id"],
                already_recorded=True,
            )
        )
    if not record:
        return TreasuryReceiptPreflight(ready=True, receipt_id=first["receipt_id"])
    return await save_receipt(
        session,
        **{
            **first,
            "selector": selector,
            "public_proof": public,
            "request_digest": request_digest,
            "proof": replace(proof, source_amount_rao=public["source_amount_rao"]),
        },
    )
