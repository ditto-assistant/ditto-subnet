"""Epoch-pinned validator ledger: one frozen fold input per chain epoch.

``GET /scoring/scores`` was a time-based read. Validators poll it once per
epoch at their own phase, so a ledger change at phase *p* split the fleet into
"read before *p*" and "read after *p*" for exactly one Yuma fold, and whichever
side lost the stake vote was clipped. The pin removes the phase from the fold
input: the first read after ``SubnetEpochIndex`` advances (or the background
loop, whichever comes first) materializes the ledger once, stores it immutably,
and every validator that reads during that epoch receives the identical bytes.

Two invariants this module keeps:

* **Never mix.** Within one epoch the platform serves either the pin or, when
  the pin cannot be produced, the previous pin flagged ``stale`` -- to every
  validator alike. The live time-based read is reached only when no pin has
  ever been taken (bootstrap).
* **Failures are never cached.** A chain read or materialization error leaves
  no state behind; the next caller retries.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ditto.api_models import LedgerEntry, LedgerResponse
from ditto.api_server.koth import koth_entries_from_ledger, project_koth
from ditto.chain.errors import ChainError
from ditto.db.queries.ledger_epochs import (
    LedgerPinDraft,
    get_pin,
    insert_pin,
    latest_pin,
)
from ditto.metrics import LEDGER_PIN_LOOP_RUNS, LEDGER_PIN_MATERIALIZATIONS

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ditto.chain.models import EpochSchedule
    from ditto.db.models import LedgerEpochSnapshot

logger = logging.getLogger(__name__)

# The chain read is one WebSocket round trip per storage item; a pin that
# cannot be keyed within this budget is retried on the next call.
DEFAULT_SCHEDULE_TIMEOUT_SECONDS = 8.0
# Two blocks. The worker commits at boundary + 270 blocks, so a pin landing
# within a few blocks of the boundary leaves the whole fleet folding it.
DEFAULT_LEDGER_PIN_LOOP_INTERVAL_SECONDS = 24.0


@dataclass(frozen=True)
class LedgerPin:
    """In-memory projection of one ``ledger_epoch_snapshots`` row."""

    netuid: int
    epoch_index: int
    last_epoch_block: int
    pinned_block: int
    pinned_block_hash: str
    pinned_at: datetime
    bench_version: int
    entries: tuple[LedgerEntry, ...]
    context: dict[str, Any]
    ledger_digest: str
    champion_agent_id: Any | None = None
    incumbent_agent_id: Any | None = None

    @classmethod
    def from_row(cls, row: LedgerEpochSnapshot) -> LedgerPin:
        pinned_at = row.pinned_at
        if pinned_at.tzinfo is None:
            pinned_at = pinned_at.replace(tzinfo=UTC)
        return cls(
            netuid=row.netuid,
            epoch_index=row.epoch_index,
            last_epoch_block=row.last_epoch_block,
            pinned_block=row.pinned_block,
            pinned_block_hash=row.pinned_block_hash,
            pinned_at=pinned_at,
            bench_version=row.bench_version,
            entries=tuple(LedgerEntry.model_validate(item) for item in row.entries),
            context=dict(row.context),
            ledger_digest=row.ledger_digest,
            champion_agent_id=row.champion_agent_id,
            incumbent_agent_id=row.incumbent_agent_id,
        )


def canonical_entries(
    entries: list[LedgerEntry] | tuple[LedgerEntry, ...],
) -> list[dict]:
    """The exact JSON the pin stores and the digest covers."""
    return [entry.model_dump(mode="json", exclude_none=True) for entry in entries]


def ledger_digest(entries_json: list[dict], served_context: dict[str, Any]) -> str:
    """SHA-256 over canonical JSON of the entries plus the served fold markers.

    Two validators folding the same pin hold the same digest; a validator
    echoing it back lets the platform show which snapshot a weight vector came
    from without re-deriving anything.
    """
    payload = json.dumps(
        {"entries": entries_json, "served": served_context},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def response_from_pin(pin: LedgerPin, *, stale: bool, now: datetime) -> LedgerResponse:
    """Replay a pin onto the validator wire exactly as it was frozen.

    Every fold marker comes from the pin's own context, never re-resolved:
    a Backroom flip after the pin was taken lands at the next pin, for the
    whole fleet at once. The requester-specific factor safety edge of the live
    path (``_snapshot_can_be_shared``) does not apply here -- the pin is built
    without a requesting hotkey, so factors on it ride purely on fleet
    readiness and are identical for every reader.
    """
    served = pin.context.get("served", {})
    age = max(0, int((now - pin.pinned_at).total_seconds()))
    return LedgerResponse(
        entries=list(pin.entries),
        active_bench_version=pin.bench_version,
        v9_confirmation_mode=served.get("v9_confirmation_mode"),
        tie_weighting_mode=served.get("tie_weighting_mode"),
        dethrone_band_mode=served.get("dethrone_band_mode"),
        count=len(pin.entries),
        generated_at=pin.pinned_at,
        stale=stale,
        age_seconds=age,
        burn_share=float(served.get("burn_share", 0.0)),
        continual_retest_cohort_size=int(served.get("continual_retest_cohort_size", 5)),
        epoch_index=pin.epoch_index,
        pinned_block=pin.pinned_block,
        pinned_at=pin.pinned_at,
        ledger_digest=pin.ledger_digest,
        crown_mode=served.get("crown_mode"),
        crown_incumbent_agent_id=(
            pin.incumbent_agent_id if served.get("crown_mode") == "incumbent" else None
        ),
    )


# Revealed weights are u16-quantized (value / sum), so two folds of the same
# recipients agree to well under a thousandth; a real tail swap moves 3% or more.
PIN_SHARE_TOLERANCE = 0.002


def pin_expected_shares(pin: Any) -> dict[str, float] | None:
    """The miner shares the pin's fold prescribes, keyed by hotkey.

    Re-runs the Platform fold over the pin's stored entries under the pin's
    frozen markers -- the same projection the validator fold produces -- and
    returns each recipient's share of the miner pool. ``None`` when the pin
    carries no positive pool.
    """
    from ditto.api_server.koth import emission_allocation

    entries = [LedgerEntry.model_validate(item) for item in (pin.entries or [])]
    context = pin.context if isinstance(pin.context, dict) else {}
    served = (
        context.get("served", {}) if isinstance(context.get("served"), dict) else {}
    )
    fold_entries = koth_entries_from_ledger(entries)
    tie_pooling = served.get("tie_weighting_mode") == "pool"
    clamp = served.get("dethrone_band_mode") == "headroom_capped"
    projection = project_koth(
        fold_entries,
        distinct_hotkeys=tie_pooling,
        ceiling_band_clamp=clamp,
        incumbent_agent_id=(
            pin.incumbent_agent_id if served.get("crown_mode") == "incumbent" else None
        ),
    )
    if projection is None:
        return None
    allocation = emission_allocation(
        fold_entries, projection, tie_pooling=tie_pooling, ceiling_band_clamp=clamp
    )
    total = sum(allocation.shares)
    if total <= 0.0:
        return None
    shares: dict[str, float] = {}
    for member, share in zip(allocation.members, allocation.shares, strict=True):
        shares[member.miner_hotkey] = (
            shares.get(member.miner_hotkey, 0.0) + share / total
        )
    return shares


def classify_vector_against_pins(
    revealed: dict[str, int | float],
    *,
    expected_current: dict[str, float] | None,
    expected_previous: dict[str, float] | None,
    burn_hotkey: str | None,
    tolerance: float = PIN_SHARE_TOLERANCE,
) -> str:
    """Whether a revealed on-chain vector matches the current or previous pin.

    The burn destination (the subnet owner hotkey) is removed before comparing,
    because the burn share is operator policy rather than a fold decision and a
    validator on an older burn setting would otherwise read as diverged on
    every miner. Remaining shares are renormalized and compared recipient by
    recipient within ``tolerance``.
    """
    miners = {
        hotkey: float(value)
        for hotkey, value in revealed.items()
        if value > 0 and hotkey != burn_hotkey
    }
    total = sum(miners.values())
    if expected_current is None or total <= 0.0:
        return "unknown"
    actual = {hotkey: value / total for hotkey, value in miners.items()}

    def matches(expected: dict[str, float]) -> bool:
        if set(expected) != set(actual):
            return False
        return all(abs(actual[h] - expected[h]) <= tolerance for h in expected)

    if matches(expected_current):
        return "current"
    if expected_previous is not None and matches(expected_previous):
        return "previous"
    return "diverged"


class LedgerPinMaterializer:
    """Single-flight producer of the pin for the chain's current epoch.

    ``ensure`` keys on ``SubnetEpochIndex``: a cached pin for the current
    epoch is returned without touching the database; otherwise one caller
    builds it under the lock while the others wait and re-read. The newest
    pin ever seen by this process is retained so an outage can still serve it
    flagged stale.
    """

    def __init__(
        self, *, schedule_timeout_seconds: float = DEFAULT_SCHEDULE_TIMEOUT_SECONDS
    ) -> None:
        self._timeout = max(1.0, schedule_timeout_seconds)
        self._lock = asyncio.Lock()
        self._current: LedgerPin | None = None

    @property
    def newest_known(self) -> LedgerPin | None:
        """The most recent pin this process has served or built."""
        return self._current

    async def read_schedule(self, app_state: Any) -> EpochSchedule | None:
        """The chain's current epoch position, or ``None`` when unreadable."""
        chain = getattr(app_state, "chain", None)
        if chain is None:
            return None
        netuid = app_state.config.chain.netuid
        try:
            async with asyncio.timeout(self._timeout):
                return await chain.read_epoch_schedule(netuid)
        except (ChainError, TimeoutError, OSError) as error:
            logger.warning(
                "epoch schedule unavailable for netuid=%s: %s", netuid, error
            )
            return None

    async def ensure(
        self,
        app_state: Any,
        session_maker: async_sessionmaker,
        *,
        now: datetime,
        schedule: EpochSchedule | None = None,
    ) -> LedgerPin | None:
        """Return the pin for the current chain epoch, building it if needed.

        ``None`` only when the chain schedule could not be read or the build
        failed; neither outcome is cached.
        """
        if schedule is None:
            schedule = await self.read_schedule(app_state)
        if schedule is None:
            return None
        cached = self._current
        if cached is not None and cached.epoch_index == schedule.subnet_epoch_index:
            return cached
        async with self._lock:
            cached = self._current
            if cached is not None and cached.epoch_index == schedule.subnet_epoch_index:
                return cached
            try:
                pin = await self._load_or_build(
                    app_state, session_maker, schedule, now=now
                )
            except SQLAlchemyError:
                LEDGER_PIN_MATERIALIZATIONS.labels(outcome="db_error").inc()
                logger.exception(
                    "ledger pin for epoch %d could not be read or written",
                    schedule.subnet_epoch_index,
                )
                return None
            except Exception:  # noqa: BLE001 - the pin path must never raise upward
                LEDGER_PIN_MATERIALIZATIONS.labels(outcome="error").inc()
                logger.exception(
                    "ledger pin for epoch %d failed to materialize",
                    schedule.subnet_epoch_index,
                )
                return None
            if pin is not None and (
                self._current is None or pin.epoch_index >= self._current.epoch_index
            ):
                self._current = pin
            return pin

    async def latest(
        self, session_maker: async_sessionmaker, *, netuid: int
    ) -> LedgerPin | None:
        """The newest durable pin, refreshing the in-memory copy on success."""
        try:
            async with session_maker() as session:
                row = await latest_pin(session, netuid=netuid)
        except SQLAlchemyError:
            return self._current
        if row is None:
            return self._current
        pin = LedgerPin.from_row(row)
        if self._current is None or pin.epoch_index >= self._current.epoch_index:
            self._current = pin
        return self._current

    async def _load_or_build(
        self,
        app_state: Any,
        session_maker: async_sessionmaker,
        schedule: EpochSchedule,
        *,
        now: datetime,
    ) -> LedgerPin | None:
        netuid = schedule.netuid
        async with session_maker() as session:
            existing = await get_pin(
                session, netuid=netuid, epoch_index=schedule.subnet_epoch_index
            )
            if existing is not None:
                LEDGER_PIN_MATERIALIZATIONS.labels(outcome="loaded").inc()
                return LedgerPin.from_row(existing)
            previous = await latest_pin(
                session, netuid=netuid, max_epoch_index=schedule.subnet_epoch_index - 1
            )
            previous_pin = (
                LedgerPin.from_row(previous) if previous is not None else None
            )
            previous_owner_root = (
                previous.champion_owner_root if previous is not None else None
            )

        # Imported here: endpoints.scoring imports this module for the serve
        # path, so a module-level import would close a cycle.
        from ditto.api_server.endpoints.scoring import (
            materialize_ledger_snapshot,
            resolve_ledger_context,
        )

        async with session_maker() as session:
            context = await resolve_ledger_context(app_state, session, now=now)
            snapshot = await materialize_ledger_snapshot(
                app_state,
                session,
                context=context,
                now=now,
                requesting_validator_hotkey=None,
            )
        draft = build_pin_draft(
            schedule,
            snapshot=snapshot,
            previous_pin=previous_pin,
            previous_champion_owner_root=previous_owner_root,
            now=now,
        )
        async with session_maker() as session:
            try:
                async with session.begin():
                    row = await insert_pin(session, draft)
                await session.refresh(row)
                LEDGER_PIN_MATERIALIZATIONS.labels(outcome="pinned").inc()
                logger.info(
                    "pinned validator ledger for epoch %d at block %d: %d miner(s), "
                    "champion=%s incumbent=%s digest=%s",
                    draft.epoch_index,
                    draft.pinned_block,
                    len(draft.entries),
                    draft.champion_agent_id,
                    draft.incumbent_agent_id,
                    draft.ledger_digest[:12],
                )
                return LedgerPin.from_row(row)
            except IntegrityError:
                # Another process pinned this epoch first; its row is the pin.
                await session.rollback()
                winner = await get_pin(
                    session, netuid=netuid, epoch_index=schedule.subnet_epoch_index
                )
                LEDGER_PIN_MATERIALIZATIONS.labels(outcome="raced").inc()
                return LedgerPin.from_row(winner) if winner is not None else None


def build_pin_draft(
    schedule: EpochSchedule,
    *,
    snapshot: Any,
    previous_pin: LedgerPin | None,
    previous_champion_owner_root: str | None,
    now: datetime,
) -> LedgerPinDraft:
    """Freeze one materialized ledger snapshot into the row the pin stores.

    ``snapshot`` is the scoring endpoint's ``_LedgerSnapshot``: the entries and
    the fold markers it would have served live at this instant, plus the
    internal owner roots needed to carry the crown across epochs.
    """
    served = {
        "v9_confirmation_mode": snapshot.v9_confirmation_mode,
        "tie_weighting_mode": snapshot.tie_weighting_mode,
        "dethrone_band_mode": snapshot.dethrone_band_mode,
        "burn_share": snapshot.burn_share,
        "continual_retest_cohort_size": snapshot.continual_retest_cohort_size,
        "crown_mode": snapshot.crown_mode,
    }
    entries_json = canonical_entries(snapshot.entries)
    owner_roots: dict = getattr(snapshot, "owner_roots", None) or {}
    incumbent = None
    if previous_champion_owner_root is not None:
        incumbent = next(
            (
                entry.agent_id
                for entry in snapshot.entries
                if owner_roots.get(entry.agent_id) == previous_champion_owner_root
            ),
            None,
        )
    elif previous_pin is not None and previous_pin.champion_agent_id is not None:
        # Legacy pin without an owner root: fall back to the exact agent.
        incumbent = next(
            (
                entry.agent_id
                for entry in snapshot.entries
                if entry.agent_id == previous_pin.champion_agent_id
            ),
            None,
        )
    projection = project_koth(
        koth_entries_from_ledger(list(snapshot.entries)),
        distinct_hotkeys=snapshot.tie_weighting_mode == "pool",
        ceiling_band_clamp=snapshot.dethrone_band_mode == "headroom_capped",
        incumbent_agent_id=(incumbent if snapshot.crown_mode == "incumbent" else None),
    )
    champion_id = projection.champion.agent_id if projection is not None else None
    context = {
        "served": served,
        "active_bench_version": snapshot.active_bench_version,
        "fleet": getattr(snapshot, "fleet_readiness", None) or {},
        "schedule": {
            "tempo": schedule.tempo,
            "pending_epoch_at": schedule.pending_epoch_at,
            "blocks_since_last_step": schedule.blocks_since_last_step,
            "next_epoch_block": schedule.next_epoch_block,
            "block_timestamp": schedule.block_timestamp,
        },
    }
    return LedgerPinDraft(
        netuid=schedule.netuid,
        epoch_index=schedule.subnet_epoch_index,
        last_epoch_block=schedule.last_epoch_block,
        pinned_block=schedule.block,
        pinned_block_hash=schedule.block_hash,
        pinned_at=now,
        bench_version=snapshot.active_bench_version,
        entries=entries_json,
        context=context,
        ledger_digest=ledger_digest(entries_json, served),
        champion_agent_id=champion_id,
        champion_owner_root=(owner_roots.get(champion_id) if champion_id else None),
        incumbent_agent_id=incumbent,
    )


class LedgerPinLoop:
    """Take the epoch pin at the boundary even when no validator is reading.

    The request path also pins on demand, but a quiet fleet must not delay the
    pin past a validator's commit phase. One loop per platform process role;
    the materializer's lock and the table's unique key make a concurrent
    request-path pin harmless.
    """

    def __init__(
        self,
        *,
        app_state: Any,
        session_maker: async_sessionmaker,
        materializer: LedgerPinMaterializer,
        interval_seconds: float = DEFAULT_LEDGER_PIN_LOOP_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("ledger pin loop interval must be positive")
        self._app_state = app_state
        self._session_maker = session_maker
        self._materializer = materializer
        self._interval_seconds = interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="ledger-pin-loop")

    async def aclose(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            await task

    async def sweep(self, *, now: datetime | None = None) -> LedgerPin | None:
        """Ensure the current epoch's pin exists; exposed for real-DB tests."""
        started = monotonic()
        settings = await self._app_state.continual_retest_settings.resolve(
            self._session_maker
        )
        if settings.ledger_pin_mode != "epoch":
            LEDGER_PIN_LOOP_RUNS.labels(outcome="disabled").inc()
            return None
        pin = await self._materializer.ensure(
            self._app_state, self._session_maker, now=now or datetime.now(UTC)
        )
        LEDGER_PIN_LOOP_RUNS.labels(outcome="pinned" if pin else "unavailable").inc()
        logger.debug("ledger pin sweep finished in %.2fs", monotonic() - started)
        return pin

    async def _run(self) -> None:
        while not self._stop.is_set():
            with suppress(Exception):
                await self.sweep()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue


__all__ = [
    "PIN_SHARE_TOLERANCE",
    "DEFAULT_LEDGER_PIN_LOOP_INTERVAL_SECONDS",
    "DEFAULT_SCHEDULE_TIMEOUT_SECONDS",
    "LedgerPin",
    "LedgerPinLoop",
    "LedgerPinMaterializer",
    "build_pin_draft",
    "canonical_entries",
    "classify_vector_against_pins",
    "pin_expected_shares",
    "ledger_digest",
    "response_from_pin",
]
