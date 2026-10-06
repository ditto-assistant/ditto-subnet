"""One manual mailbox claim at a time; unknown delivery uses the old journal."""

import json
import time

from ditto.treasury.collector import (
    ManualTransfer,
    arm_manual_transfer,
    manual_transfer_readiness,
    tick,
)
from ditto_screening_protocol.treasury_manual import (
    ManualEnvelope,
    ManualReadiness,
    ManualReport,
    ManualSettlement,
)


def publish_readiness(mailbox, journal, policy, chain):
    readiness = ManualReadiness.model_validate(
        manual_transfer_readiness(journal, policy, chain)
    )
    mailbox.publish(
        ManualReport(
            collector_policy_digest=policy.digest,
            observed_at=int(time.time()),
            readiness=readiness,
            status="readiness",
        ).model_dump()
    )


def process_manual(mailbox, journal, policy, chain, ack_id, body):
    """ACK only after terminal public coordinates reach the return mailbox.

    A crash at any point redelivers this same UUID. The existing journal binds
    exact request bytes and persists signed bytes before any network submission.
    No exception path creates a replacement claim or resets the journal.
    """
    envelope = ManualEnvelope.model_validate(body)
    request = envelope.request
    if envelope.collector_policy_digest != policy.digest or not any(
        d.bucket_id == request.bucket_id
        and d.allocation_bps > 0
        and d.holding_coldkey == envelope.destination
        for d in policy.destinations
    ):
        raise ValueError("mailbox request differs from signed custody pin")
    manual = ManualTransfer(**request.model_dump())
    try:
        arm_manual_transfer(journal, policy, chain, manual)
    except ValueError:
        # A malformed/expired/stale request may be refused only before it was
        # armed. An already-armed operation must stay pending for reconciliation.
        if journal.db.execute(
            "SELECT 1 FROM events WHERE event='manual_transfer_armed' "
            "AND json_extract(payload,'$.request_id')=?",
            (request.request_id,),
        ).fetchone():
            raise
        report = ManualReport(
            collector_policy_digest=policy.digest,
            observed_at=int(time.time()),
            request_id=request.request_id,
            request_digest=envelope.digest,
            status="refused",
        )
    else:
        row = journal.db.execute(
            "SELECT * FROM operations WHERE id>? ORDER BY id LIMIT 1",
            (request.after_operation,),
        ).fetchone()
        # A terminal old message can redeliver after a newer request was armed.
        # Re-export its exact claim; never execute the newer intent for it.
        result = None
        if row is None or row["state"] == "dispatching":
            result = tick(
                journal, policy, chain, "transfer", manual_request_id=request.request_id
            )
            row = journal.db.execute(
                "SELECT * FROM operations WHERE id>? ORDER BY id LIMIT 1",
                (request.after_operation,),
            ).fetchone()
        state = row["state"] if row else None
        settlement = None
        if state == "finalized":
            saved = json.loads(row["settlement_json"])
            source_hash = chain.substrate.get_block_hash(request.source_block)
            epoch = chain.query(
                "SubtensorModule", "SubnetEpochIndex", [118], source_hash
            )
            settlement = ManualSettlement(
                epoch_index=epoch,
                **{
                    k: saved[k]
                    for k in (
                        "block",
                        "block_hash",
                        "extrinsic_index",
                        "extrinsic_hash",
                    )
                },
            )
        report = ManualReport(
            collector_policy_digest=policy.digest,
            observed_at=int(time.time()),
            request_id=request.request_id,
            request_digest=envelope.digest,
            status="finalized"
            if state == "finalized"
            else (
                "failed"
                if state in {"failed", "expired"} or result == "manual_expired"
                else "pending"
            ),
            settlement=settlement,
        )
    mailbox.publish(report.model_dump())
    if report.status != "pending":
        mailbox.ack(ack_id)
    return report
