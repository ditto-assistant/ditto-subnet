"""Explicitly retire an expired intent that never reserved signed bytes."""

import hashlib
import json
from dataclasses import asdict

from ditto.treasury.collector import canonical, manual_transfer_history


def expire_unsigned_manual(journal, policy, chain, request_id, *, reason):
    """Append audit history only. Never signs, sends, or queues a replacement."""
    if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 240:
        raise ValueError("bounded operator recovery reason required")
    db = journal.db
    db.execute("BEGIN IMMEDIATE")
    try:
        pinned = db.execute("SELECT digest,role FROM pin").fetchone()
        if pinned is None or tuple(pinned) != (
            policy.digest,
            "transfer",
        ):
            raise ValueError("recovery requires current pinned transfer journal")
        try:
            history = manual_transfer_history(journal, policy)
        except (KeyError, TypeError) as error:
            raise ValueError("invalid armed manual history") from error
        if not history or history[-1].request_id != request_id:
            raise ValueError("recovery requires exact latest armed request")
        request = history[-1]
        armed = db.execute(
            "SELECT id,payload FROM events WHERE event='manual_transfer_armed' "
            "AND json_extract(payload,'$.request_id')=?",
            (request_id,),
        ).fetchone()
        armed_payload = json.loads(armed["payload"]) if armed is not None else None
        if (
            not isinstance(armed_payload, dict)
            or armed_payload.get("policy") != policy.digest
        ):
            raise ValueError("recovery cannot cross a policy migration")
        maximum = db.execute("SELECT COALESCE(MAX(id),0) FROM operations").fetchone()[0]
        prior = db.execute(
            "SELECT role,state FROM operations WHERE id=?", (maximum,)
        ).fetchone()
        if (
            maximum != request.after_operation
            or prior is None
            or tuple(prior) != ("transfer", "finalized")
            or db.execute(
                "SELECT 1 FROM events WHERE event='dispatch_reserved' AND id>? LIMIT 1",
                (armed["id"],),
            ).fetchone()
        ):
            raise ValueError("signed or unresolved claim requires chain reconciliation")
        observed = chain.observe(policy, "transfer")
        if observed.block < request.expires_block:
            raise ValueError("request has not reached finalized expiry")
        proof = {
            "policy": policy.digest,
            "request_id": request_id,
            "request_digest": hashlib.sha256(
                canonical(asdict(request)).encode()
            ).hexdigest(),
            "after_operation": maximum,
            "finalized_block": observed.block,
            "finalized_block_hash": observed.block_hash,
            "reason": reason.strip(),
        }
        journal.event("manual_unsigned_expired", proof)
        manual_transfer_history(journal, policy)
        db.execute("COMMIT")
        return proof
    except BaseException:
        db.execute("ROLLBACK")
        raise
