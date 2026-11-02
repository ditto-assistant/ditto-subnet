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
from ditto.api_models.treasury_settings import TreasurySettings
from ditto.db.models import TreasuryRuntimeRevision as RuntimeRow
from ditto.db.models import TreasurySettingsRevision as SettingsRow
from ditto_screening_protocol.treasury import TreasuryEmissionPolicy
from ditto_screening_protocol.treasury_approval import (
    TreasuryPolicyApproval,
    verify_policy_approval,
    verify_public_signature,
)
from ditto_screening_protocol.treasury_enforcement import TreasuryBucketAllocation


@dataclass(frozen=True)
class TreasuryRuntime:
    revision: int
    treasury_shadow_policy: TreasuryEmissionPolicy | None
    treasury_shadow_approval: TreasuryPolicyApproval | None
    treasury_approved_policy_digest: str | None
    treasury_approved_collector_policy_digest: str | None
    treasury_weight_enforcement: bool
    activation_epoch: int | None = None
    treasury_managed_validator_hotkeys: tuple[str, ...] = ()


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
            treasury_managed_validator_hotkeys=tuple(
                getattr(config, "treasury_managed_validator_hotkeys", ())
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
        treasury_managed_validator_hotkeys=s.managed_validator_hotkeys,
    )


def settings_match_policy(
    settings: TreasurySettings, policy: TreasuryEmissionPolicy
) -> bool:
    """Same collector and wallets, each Gamma % inside its coldkey-signed range.

    A fixed signed bucket admits only its exact value, so fixed policies keep
    the original exact-match rule.
    """
    return (
        settings.allocation_version == 2
        and (settings.treasury_hotkey, settings.treasury_coldkey)
        == (policy.collector_hotkey, policy.collector_coldkey)
        and sorted((b.bucket_id, b.holding_coldkey) for b in settings.service_buckets)
        == sorted((b.bucket_id, b.holding_coldkey) for b in policy.buckets)
        and policy.admits(
            {b.bucket_id: b.allocation_bps for b in settings.service_buckets}
        )
    )


def gamma_only_change(previous: TreasurySettings, new: TreasurySettings) -> bool:
    """True when two revisions differ in nothing but bucket allocation values."""

    def without_allocation(settings: TreasurySettings) -> dict:
        raw = settings.model_dump(mode="json")
        for bucket in raw["service_buckets"]:
            bucket["allocation_bps"] = 0
        return raw

    return without_allocation(previous) == without_allocation(new)


async def latest_settings(session: AsyncSession) -> TreasurySettings | None:
    row = await session.scalar(
        select(SettingsRow).order_by(SettingsRow.revision.desc()).limit(1)
    )
    if row is None:
        return None
    if canonical_settings(row.settings) != row.checksum:
        raise ValueError("stored treasury settings checksum mismatch")
    return TreasurySettings.model_validate(row.settings)


async def pinned_allocations(
    session: AsyncSession, policy: TreasuryEmissionPolicy
) -> tuple[TreasuryBucketAllocation, ...] | None:
    """The operator's in-range Gamma allocation an enforcing epoch freezes.

    A fixed policy pins nothing: its signature already names the allocation.
    """
    if not policy.ranged:
        return None
    settings = await latest_settings(session)
    if settings is None or not settings_match_policy(settings, policy):
        raise ValueError("Gamma allocation is outside the signed policy range")
    return tuple(
        TreasuryBucketAllocation(bucket_id=b.bucket_id, allocation_bps=b.allocation_bps)
        for b in sorted(settings.service_buckets, key=lambda b: b.bucket_id)
    )
