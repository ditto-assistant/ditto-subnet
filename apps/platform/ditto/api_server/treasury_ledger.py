"""Per-epoch Gamma collector sampling and the public ledger built from it.

The sampler stores one finalized public reading of the collector per epoch.
The ledger compares what the collector earned with how its balance moved:
an outflow is attributed by a verified treasury receipt, and the remainder
stays visible as unattributed until Backroom records that receipt.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_ledger import (
    PublicTreasuryLedger,
    TreasuryLedgerCurrent,
    TreasuryLedgerHoldingView,
    TreasuryLedgerInterval,
    TreasuryLedgerSamplerStatus,
    TreasuryLedgerTotals,
)
from ditto.api_models.treasury_settings import TreasurySettings
from ditto.chain.treasury_ledger_read import TreasuryLedgerReading, read_treasury_ledger
from ditto.db.models import (
    TreasuryLedgerSample,
    TreasuryPublicEvent,
    TreasurySettingsRevision,
)

logger = logging.getLogger(__name__)

# Balance moves below this are dust, never an unattributed outflow.
DUST_RAO = 1_000_000
SamplerState = Literal["running", "not_configured", "blocked", "stopped"]
LockStatus = Literal["none", "perpetual", "decaying"]


def sample_row(reading: TreasuryLedgerReading) -> dict[str, Any]:
    return {
        "netuid": reading.netuid,
        "epoch_index": reading.epoch_index,
        "block_number": reading.block_number,
        "block_hash": reading.block_hash,
        "block_at": datetime.fromtimestamp(reading.block_timestamp_ms / 1000, UTC),
        "collector_hotkey": reading.collector_hotkey,
        "collector_coldkey": reading.collector_coldkey,
        "collector_uid": reading.collector_uid,
        "collector_incentive": reading.collector_incentive,
        "collector_emission_rao": reading.collector_emission_rao,
        "collector_total_rao": reading.collector_total_rao,
        "collector_locked_rao": reading.collector_locked_rao,
        "collector_available_rao": reading.collector_available_rao,
        "collector_lock_perpetual": reading.collector_lock_perpetual,
        "owner_hotkey": reading.owner_hotkey,
        "owner_conviction_rao": reading.owner_conviction_rao,
        "most_convicted_hotkey": reading.most_convicted_hotkey,
        "holdings": [asdict(holding) for holding in reading.holdings],
    }


async def record_sample(session: AsyncSession, reading: TreasuryLedgerReading) -> bool:
    """Insert the epoch's first reading; a later reading of it is ignored."""
    result = await session.execute(
        insert(TreasuryLedgerSample)
        .values(**sample_row(reading))
        .on_conflict_do_nothing(constraint="treasury_ledger_samples_netuid_epoch")
        .returning(TreasuryLedgerSample.id)
    )
    return result.scalar_one_or_none() is not None


async def latest_policy(session: AsyncSession) -> TreasurySettings:
    row = await session.scalar(
        select(TreasurySettingsRevision)
        .order_by(TreasurySettingsRevision.revision.desc())
        .limit(1)
    )
    return TreasurySettings.model_validate(row.settings) if row else TreasurySettings()


class TreasuryLedgerSampler:
    """Read the collector once per tick; store the first reading per epoch."""

    def __init__(
        self,
        *,
        app_state: Any,
        session_maker: Any,
        enabled: bool = True,
        interval_seconds: float = 300.0,
    ) -> None:
        self.state = app_state
        self.sessions = session_maker
        self.enabled = enabled
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.status: SamplerState = "stopped"
        self.last_sampled_at: datetime | None = None
        self.last_error: str | None = None

    async def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._run(), name="treasury-ledger")

    async def aclose(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.status = "stopped"

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as error:
                self.status = "blocked"
                # Class name only: provider URLs and credentials stay private.
                self.last_error = type(error).__name__
                logger.warning("treasury ledger sample blocked: %r", error)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self.interval_seconds)

    async def read(self, policy: TreasurySettings) -> TreasuryLedgerReading:
        from async_substrate_interface import AsyncSubstrateInterface

        assert policy.treasury_hotkey and policy.treasury_coldkey
        holdings = [
            (bucket.bucket_id, bucket.holding_coldkey)
            for bucket in policy.service_buckets
            if bucket.holding_coldkey
        ]
        async with asyncio.timeout(120):
            async with AsyncSubstrateInterface(
                url=self.state.chain._substrate_url()
            ) as substrate:
                return await read_treasury_ledger(
                    substrate,
                    netuid=self.state.config.chain.netuid,
                    collector_hotkey=policy.treasury_hotkey,
                    collector_coldkey=policy.treasury_coldkey,
                    holdings=holdings,
                )

    async def tick(self) -> bool:
        async with self.sessions() as session:
            policy = await latest_policy(session)
        if not policy.treasury_hotkey or not policy.treasury_coldkey:
            self.status = "not_configured"
            return False
        reading = await self.read(policy)
        async with self.sessions() as session, session.begin():
            inserted = await record_sample(session, reading)
        self.status = "running"
        self.last_error = None
        self.last_sampled_at = datetime.now(UTC)
        return inserted

    def public_status(self) -> TreasuryLedgerSamplerStatus:
        return TreasuryLedgerSamplerStatus(
            enabled=self.enabled,
            state=self.status,
            last_sampled_at=self.last_sampled_at,
            last_error=self.last_error,
        )


def _lock_status(sample: TreasuryLedgerSample) -> LockStatus:
    if sample.collector_locked_rao <= 0:
        return "none"
    return "perpetual" if sample.collector_lock_perpetual else "decaying"


def _current(sample: TreasuryLedgerSample) -> TreasuryLedgerCurrent:
    return TreasuryLedgerCurrent(
        epoch_index=sample.epoch_index,
        block_number=sample.block_number,
        block_hash=sample.block_hash,
        block_at=sample.block_at,
        collector_hotkey=sample.collector_hotkey,
        collector_coldkey=sample.collector_coldkey,
        collector_uid=sample.collector_uid,
        collector_incentive=sample.collector_incentive,
        earned_per_epoch_rao=sample.collector_emission_rao,
        total_rao=sample.collector_total_rao,
        locked_rao=sample.collector_locked_rao,
        available_rao=sample.collector_available_rao,
        lock_status=_lock_status(sample),
        owner_hotkey=sample.owner_hotkey,
        owner_conviction_rao=sample.owner_conviction_rao,
        owner_holds_most_conviction=(
            sample.most_convicted_hotkey == sample.owner_hotkey
            if sample.owner_hotkey and sample.most_convicted_hotkey
            else None
        ),
        holdings=[
            TreasuryLedgerHoldingView.model_validate(item) for item in sample.holdings
        ],
    )


def _interval(
    current: TreasuryLedgerSample,
    previous: TreasuryLedgerSample,
    receipts: list[TreasuryPublicEvent],
) -> TreasuryLedgerInterval:
    gap = current.epoch_index - previous.epoch_index
    per_epoch = current.collector_emission_rao
    # Emission is the last epoch's credit; a skipped epoch is extrapolated.
    earned = per_epoch * gap if per_epoch is not None else None
    change = current.collector_total_rao - previous.collector_total_rao
    outflow = max(0, (earned or 0) - change)
    window = [
        receipt
        for receipt in receipts
        if previous.block_at < receipt.event_at <= current.block_at
    ]
    attributed = sum(receipt.allocated_alpha_rao for receipt in window)
    unattributed = max(0, outflow - attributed)
    return TreasuryLedgerInterval(
        epoch_index=current.epoch_index,
        previous_epoch_index=previous.epoch_index,
        block_number=current.block_number,
        block_at=current.block_at,
        earned_rao=earned,
        estimated=gap > 1,
        balance_change_rao=change,
        outflow_rao=outflow if outflow > DUST_RAO else 0,
        attributed_rao=attributed,
        unattributed_rao=unattributed if unattributed > DUST_RAO else 0,
        receipt_ids=[receipt.id for receipt in window],
    )


async def ledger_view(
    session: AsyncSession,
    *,
    netuid: int,
    limit: int,
    sampler: TreasuryLedgerSamplerStatus,
) -> PublicTreasuryLedger:
    """The newest ``limit`` intervals, newest first, with window totals."""
    samples = list(
        (
            await session.scalars(
                select(TreasuryLedgerSample)
                .where(TreasuryLedgerSample.netuid == netuid)
                .order_by(TreasuryLedgerSample.epoch_index.desc())
                .limit(limit + 1)
            )
        ).all()
    )
    if not samples:
        return PublicTreasuryLedger(
            current=None,
            totals=TreasuryLedgerTotals(
                window_epochs=0,
                earned_rao=0,
                outflow_rao=0,
                attributed_rao=0,
                unattributed_rao=0,
            ),
            sampler=sampler,
        )
    newest, oldest = samples[0], samples[-1]
    receipts = list(
        (
            await session.scalars(
                select(TreasuryPublicEvent).where(
                    TreasuryPublicEvent.state == "chain_finalized",
                    TreasuryPublicEvent.public_sender.in_(
                        {sample.collector_coldkey for sample in samples}
                    ),
                    TreasuryPublicEvent.event_at > oldest.block_at,
                    TreasuryPublicEvent.event_at <= newest.block_at,
                )
            )
        ).all()
    )
    intervals = [
        _interval(current, previous, receipts)
        for current, previous in zip(samples, samples[1:], strict=False)
        # A rotated collector is a new account, not an outflow.
        if current.collector_coldkey == previous.collector_coldkey
    ]
    return PublicTreasuryLedger(
        current=_current(newest),
        totals=TreasuryLedgerTotals(
            window_epochs=newest.epoch_index - oldest.epoch_index,
            earned_rao=sum(item.earned_rao or 0 for item in intervals),
            outflow_rao=sum(item.outflow_rao for item in intervals),
            attributed_rao=sum(item.attributed_rao for item in intervals),
            unattributed_rao=sum(item.unattributed_rao for item in intervals),
        ),
        intervals=intervals,
        sampler=sampler,
    )


def sampler_status(app_state: Any) -> TreasuryLedgerSamplerStatus:
    sampler = getattr(app_state, "treasury_ledger_sampler", None)
    if sampler is None:
        return TreasuryLedgerSamplerStatus(enabled=False, state="stopped")
    return sampler.public_status()
