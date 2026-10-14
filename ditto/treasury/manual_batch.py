"""One durable transfer backed by multiple independently proved earnings.

The base custody policy and journal pin do not change. A separate cold approval
only expands manual-once amounts; automatic ticks retain their old ceiling.
"""

import json
from dataclasses import asdict, dataclass

from ditto.treasury.collector import (
    FinalizedEarnings,
    ManualIntentRefused,
    ManualTransfer,
    canary_replacement,
    remaining_distribution,
)
from ditto.treasury.service_allocation import plan_service_distribution
from ditto_screening_protocol.treasury_manual import ManualBatchRequest
from ditto_screening_protocol.treasury_manual_limit import ManualLimitApproval


@dataclass(frozen=True)
class ManualBatchTransfer(ManualTransfer):
    sources: list[dict]
    manual_approval: dict

    def __post_init__(self):
        super().__post_init__()
        ManualBatchRequest.model_validate(
            {k: v for k, v in asdict(self).items() if k != "manual_approval"}
        )


def manual_maximum(request, policy, *, collector_digest=None):
    if isinstance(request, ManualBatchTransfer):
        return (
            ManualLimitApproval.model_validate(request.manual_approval)
            .verify(collector_digest or policy.digest, policy.collector_coldkey)
            .max_amount_rao
        )
    return policy.max_distribution_rao


def source_operations(journal, block, *, exclude=-1):
    """Project multi-source consumption without altering historical operations."""
    rows = journal.db.execute(
        "SELECT payload FROM events WHERE event='manual_transfer_armed' "
        "AND json_type(payload,'$.sources')='array' ORDER BY id"
    ).fetchall()
    claims = {}
    for row in rows:
        body = json.loads(row[0])
        request = ManualBatchTransfer(
            **{k: v for k, v in body.items() if k != "policy"}
        )
        operation_id = request.after_operation + 1
        if operation_id in claims:
            raise ValueError("ambiguous aggregate operation attribution")
        claims[operation_id] = request
    result = []
    for operation in journal.db.execute(
        "SELECT * FROM operations WHERE id!=? ORDER BY id", (exclude,)
    ):
        bound_request = claims.get(operation["id"])
        if bound_request:
            if (
                operation["role"] != "transfer"
                or operation["amount"] != bound_request.amount_rao
                or operation["bucket"] != bound_request.bucket_id
                or operation["source_block"] != bound_request.source_block
            ):
                raise ValueError("aggregate operation differs from durable intent")
            for claim in bound_request.sources:
                if claim["source_block"] == block:
                    result.append({**dict(operation), "amount": claim["amount_rao"]})
        elif operation["source_block"] == block:
            result.append(operation)
    return result


def remaining_source(journal, policy, row, *, manual=False):
    if not manual and row["amount"] > policy.max_distribution_rao:
        raise ValueError("source exceeds unchanged signed receipt batch")
    parts = plan_service_distribution(
        attributed_alpha_rao=row["amount"],
        available_alpha_rao=row["amount"],
        collector_coldkey=policy.collector_coldkey,
        destinations=policy.destinations,
    )
    replacement = canary_replacement(journal, policy)
    return remaining_distribution(
        parts,
        source_operations(
            journal,
            row["block"],
            exclude=replacement["after_operation"] if replacement else -1,
        ),
    )


def validate_batch(journal, policy, chain, request, observed):
    # Revalidate at the spending boundary too: a frozen dataclass can contain
    # mutable lists in an in-process caller, despite constructor validation.
    checked = ManualBatchRequest.model_validate(
        {k: v for k, v in asdict(request).items() if k != "manual_approval"}
    )
    approved = ManualLimitApproval.model_validate(request.manual_approval)
    approved.verify(policy.digest, policy.collector_coldkey)
    active = getattr(chain, "manual_approval", None)
    if active != approved:
        raise ManualIntentRefused("manual approval differs from deployed exact pin")
    if request.amount_rao > approved.policy.max_amount_rao:
        raise ManualIntentRefused("manual amount exceeds cold-approved range")
    if request.amount_rao + request.retained_alpha_rao > observed.alpha_rao:
        raise ManualIntentRefused("manual request would consume retained stake")
    destinations = [
        d.holding_coldkey
        for d in policy.destinations
        if d.bucket_id == checked.bucket_id and d.allocation_bps > 0
    ]
    if len(destinations) != 1:
        raise ManualIntentRefused("manual bucket must have one signed destination")
    destination = destinations[0]
    for claim in checked.sources:
        block, amount = claim.source_block, claim.amount_rao
        if block < policy.start_block or (
            block + policy.distribution_interval_blocks > observed.block
        ):
            raise ManualIntentRefused("aggregate source is immature or unapproved")
        row = journal.db.execute(
            "SELECT * FROM earnings WHERE block=? AND completed=0", (block,)
        ).fetchone()
        if row is None:
            raise ManualIntentRefused("aggregate source receipt absent")
        if chain.earnings(policy, block) != FinalizedEarnings(
            row["amount"], row["block_hash"], row["event_digest"]
        ):
            raise ValueError("aggregate source proof changed or unavailable")
        remaining = remaining_source(journal, policy, row, manual=True)
        if not any(
            p.bucket_id == checked.bucket_id
            and p.holding_coldkey == destination
            and p.alpha_rao >= amount
            for p in remaining
        ):
            raise ManualIntentRefused("aggregate source entitlement exceeded")
    return destination
