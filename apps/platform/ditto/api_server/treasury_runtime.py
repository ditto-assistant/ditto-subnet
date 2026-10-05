"""Read durable controls on each producer/request path, never mutate app config."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_runtime import (
    TreasuryRuntimeRevision,
    TreasuryRuntimeSettings,
)
from ditto.db.models import TreasuryRuntimeRevision as RuntimeRow
from ditto_screening_protocol.treasury import TreasuryEmissionPolicy
from ditto_screening_protocol.treasury_approval import (
    TreasuryPolicyApproval,
    verify_policy_approval,
    verify_public_signature,
)


@dataclass(frozen=True)
class TreasuryRuntime:
    revision: int
    treasury_shadow_policy: TreasuryEmissionPolicy | None
    treasury_shadow_approval: TreasuryPolicyApproval | None
    treasury_approved_policy_digest: str | None
    treasury_approved_collector_policy_digest: str | None
    treasury_weight_enforcement: bool
    activation_epoch: int | None = None


def canonical_settings(settings: dict) -> str:
    return hashlib.sha256(
        json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def runtime_revision(row: RuntimeRow) -> TreasuryRuntimeRevision:
    if canonical_settings(row.settings) != row.checksum:
        raise ValueError("stored runtime control checksum mismatch")
    settings = TreasuryRuntimeSettings.model_validate(row.settings)
    verify_policy_approval(
        settings.approval,
        expected_policy_digest=settings.approved_policy_digest,
        expected_collector_policy_digest=settings.collector_policy_digest,
        verify_signature=verify_public_signature,
    )
    return TreasuryRuntimeRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        settings=settings,
        checksum=row.checksum,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


async def latest_runtime_row(session: AsyncSession) -> RuntimeRow | None:
    return await session.scalar(
        select(RuntimeRow).order_by(RuntimeRow.revision.desc()).limit(1)
    )


async def lock_runtime(session: AsyncSession) -> None:
    # One transaction-scoped lock shared by control writes and epoch insert.
    await session.execute(text("SELECT pg_advisory_xact_lock(118, 2706)"))


async def treasury_runtime(session: AsyncSession, config: Any) -> TreasuryRuntime:
    row = await latest_runtime_row(session)
    if row is None:
        return TreasuryRuntime(
            revision=0,
            treasury_shadow_policy=getattr(config, "treasury_shadow_policy", None),
            treasury_shadow_approval=getattr(config, "treasury_shadow_approval", None),
            treasury_approved_policy_digest=getattr(
                config, "treasury_approved_policy_digest", None
            ),
            treasury_approved_collector_policy_digest=getattr(
                config, "treasury_approved_collector_policy_digest", None
            ),
            treasury_weight_enforcement=getattr(
                config, "treasury_weight_enforcement", False
            ),
        )
    s = runtime_revision(row).settings
    return TreasuryRuntime(
        revision=row.revision,
        treasury_shadow_policy=s.approval.policy,
        treasury_shadow_approval=s.approval,
        treasury_approved_policy_digest=s.approved_policy_digest,
        treasury_approved_collector_policy_digest=s.collector_policy_digest,
        treasury_weight_enforcement=s.mode == "enforce",
        activation_epoch=s.activation_epoch,
    )
