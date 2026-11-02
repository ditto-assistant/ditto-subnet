"""Append-only attribution evidence from authenticated v27 heartbeats.

Only the signature-verified heartbeat endpoint calls this writer. A queued
Pylon fold is NOT itself an emission receipt; the payout observer must still
match it to historical chain state before permitting any source disclosure.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.validator import ValidatorHeartbeatRequest
from ditto.db.models import ValidatorWeightsFoldHistory


async def record_verified_weights_fold(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    heartbeat: ValidatorHeartbeatRequest,
    now: datetime,
) -> None:
    fold = heartbeat.weights_fold
    if heartbeat.protocol_version < 27 or fold is None:
        return
    payload = fold.model_dump(mode="json", exclude_none=True)
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    await session.execute(
        insert(ValidatorWeightsFoldHistory)
        .values(
            validator_hotkey=validator_hotkey,
            fold_digest=digest,
            folded_at=fold.folded_at,
            vector_digest=fold.vector_digest,
            epoch_index=fold.epoch_index,
            ledger_digest=fold.ledger_digest,
            champion_agent_id=fold.champion_agent_id,
            weights_fold=payload,
            first_seen_at=now,
            signature=heartbeat.signature,
            signed_heartbeat=heartbeat.model_dump(mode="json", exclude_none=True),
        )
        .on_conflict_do_nothing(index_elements=["validator_hotkey", "fold_digest"])
    )
