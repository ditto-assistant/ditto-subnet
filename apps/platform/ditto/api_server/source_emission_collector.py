"""Durable finalized-chain attribution; unidentified vectors never reveal source."""

from __future__ import annotations

import asyncio
import logging
import random
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from ditto.api_models.weight_receipt import FinalizedWeightReceipt
from ditto.api_server.ledger_pin import (
    LedgerPin,
    classify_vector_against_pins,
    pin_expected_burn,
    pin_expected_shares,
    pin_expected_treasury_vector,
)
from ditto.chain.models import (
    ChainMinerEarning,
    ChainMinerEmissionReceipt,
    ChainWeight,
    ChainWeightVector,
)
from ditto.chain.source_emission_verifier import (
    SourceEmissionBlock,
    SourceEmissionReadCache,
    VotingStake,
    WinnerBacking,
    evaluate_winner_payout,
    plan_source_emission_range,
    plan_source_emission_range_by_reveals,
    read_source_emission_block,
    vector_digest,
)
from ditto.db.models import (
    Agent,
    AgentKingship,
    LedgerEpochSnapshot,
    SourceEmissionCollectorCursor,
    SourceEmissionPayout,
    SourceEmissionPayoutResolution,
)
from ditto.db.queries.king_reign import KingEmissionProof, record_emission_confirmed
from ditto.db.queries.source_emission_collector import (
    advance_source_emission_cursor,
    list_source_emission_vector_bindings,
    record_reveal_plan_shadow,
    record_source_emission_payout,
    skip_quiet_source_emission_blocks,
    upsert_source_emission_vector_binding,
)
from ditto.db.queries.weight_receipts import get_finalized_weight_receipts

logger = logging.getLogger(__name__)

# Steady state advances ~2 blocks per 30s sweep. A stalled collector (unaudited
# runtime fingerprint, provider outage) can fall hundreds of thousands of blocks
# behind; at the steady-state batch size that replay takes weeks and every day
# of delay keeps crowned kings permanently embargoed. Past a small backlog the
# sweep widens its batch and skips the leading payout resolution, which re-reads
# the same historical evidence every sweep and is re-run at the end of each
# successful batch anyway.
_SWEEP_BATCH_BLOCKS = 16
_SWEEP_CATCHUP_BATCH_BLOCKS = 128
_SWEEP_CATCHUP_MIN_BACKLOG = 1000
# Each reopened payout re-reads a full historical receipt. Bound that work so a
# rescan cannot consume the sweep deadline that also covers the block scan.
_RECEIPT_REFETCH_LIMIT = 5
# Range planner for catch-up. "shadow": the event planner drives the cursor and
# the reveal-anchored planner is compared against what the full reader acted on
# (Backroom release_gate.reveal_plan_shadow). "primary": the reveal-anchored
# planner drives it. Promote only after a long run of shadowed ranges with no
# miss; docs/source-runtime-interface.md has the criterion.
_REVEAL_PLAN_MODE = "shadow"
# A shadow comparison must never starve the sweep that hosts it: it runs after
# payout resolution, inside what is left of the sweep budget minus a reserve.
_REVEAL_PLAN_SHADOW_SECONDS = 90.0
_REVEAL_PLAN_SHADOW_RESERVE_SECONDS = 15.0
_REVEAL_PLAN_SHADOW_MIN_SECONDS = 10.0
# Sparse replay reads one storage call per quiet block and the full reader only
# for the few blocks that can matter, so a catch-up range can span two tempos.
_SWEEP_SPARSE_BATCH_BLOCKS = 720
# Quiet blocks re-read in full per sparse range; any disagreement fails closed.
_SPARSE_AUDIT_SAMPLES = 2


def _receipt_from_json(data: dict) -> ChainMinerEmissionReceipt:
    return ChainMinerEmissionReceipt(
        **{
            key: data[key]
            for key in (
                "netuid",
                "block",
                "block_hash",
                "block_timestamp",
                "epoch_index",
                "previous_distribution_block",
                "owner_hotkey",
            )
        },
        earnings=tuple(ChainMinerEarning(**item) for item in data["earnings"]),
        vectors=tuple(
            ChainWeightVector(
                validator_uid=item["validator_uid"],
                validator_hotkey=item["validator_hotkey"],
                weights=tuple(ChainWeight(**weight) for weight in item["weights"]),
            )
            for item in data["vectors"]
        ),
        validator_last_updates=tuple(
            tuple(item) for item in data["validator_last_updates"]
        ),
        validator_last_update_timestamps=tuple(
            tuple(item) for item in data["validator_last_update_timestamps"]
        ),
    )


class _CallCounter:
    """Counts the chain calls a planner makes, for the shadow's cost figure."""

    _COUNTED = frozenset({"rpc_request", "query", "query_map", "get_block_metadata"})

    def __init__(self, substrate: Any) -> None:
        self._substrate = substrate
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._substrate, name)
        if name not in self._COUNTED:
            return attr

        async def counted(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return await attr(*args, **kwargs)

        return counted


class SourceEmissionCollector:
    """Collect while disclosure is paused; confirmation is independently gated.

    A payout snapshot retains its consumed vector provenance even when a signed
    Pylon claim arrives later. Replay resolves that snapshot, never today's
    crown or today's vector. Pending payouts cycle fairly by last_checked_at.
    """

    def __init__(
        self,
        *,
        app_state: Any,
        session_maker: Any,
        confirmation_enabled: bool = True,
        interval_seconds: float = 30.0,
    ) -> None:
        self.state = app_state
        self.sessions = session_maker
        self.confirmation_enabled = confirmation_enabled
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_error: str | None = None
        self._verified_commits: set[str] = set()
        # Set at the start of each provider sweep; a deep backlog widens the
        # scan batch and its timeout, then decays back to steady state.
        self._backlog_deep = False
        # (first, last, blocks the full reader acted on) of the last sparse
        # range, compared once payouts for the sweep have been resolved.
        self._pending_shadow: tuple[int, int, list[int]] | None = None

    @property
    def netuid(self) -> int:
        return self.state.config.chain.netuid

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name="source-emission-collector"
            )

    async def aclose(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.sweep()
                self.last_error = None
            except Exception as error:
                self.last_error = f"{type(error).__name__}: {str(error)[:300]}"
                logger.warning("source emission collector blocked: %s", self.last_error)
                try:
                    async with self.sessions() as session, session.begin():
                        await session.execute(
                            update(SourceEmissionCollectorCursor)
                            .where(
                                SourceEmissionCollectorCursor.netuid == self.netuid,
                            )
                            .values(last_blocked_reason=self.last_error)
                        )
                except Exception:
                    logger.exception("source emission collector status write failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self.interval_seconds)

    async def sweep(self) -> None:
        from async_substrate_interface import AsyncSubstrateInterface

        failures: list[str] = []
        for url in self.state.chain._historical_substrate_urls()[:3]:
            try:
                async with asyncio.timeout(self._sweep_timeout_seconds()) as budget:
                    async with AsyncSubstrateInterface(url=url) as substrate:
                        await self._sweep_provider(substrate, budget=budget)
                return
            except Exception as error:
                failures.append(self.state.chain._safe_rpc_error(error)[:200])
        raise RuntimeError(
            "all source-emission archive providers failed: " + "; ".join(failures)
        )

    def _sweep_timeout_seconds(self) -> float:
        """Scale the sweep budget with the batch a deep backlog will scan."""
        return 600.0 if self._backlog_deep else 120.0

    async def _sweep_provider(
        self, substrate: Any, *, budget: asyncio.Timeout | None = None
    ) -> None:
        finalized_hash = await substrate.get_chain_finalised_head()
        header = await substrate.get_block_header(block_hash=finalized_hash)
        number = header.get("header", header)["number"]
        finalized = int(number, 16) if isinstance(number, str) else int(number)
        async with self.sessions() as session:
            cursor = await session.get(SourceEmissionCollectorCursor, self.netuid)
            current = cursor.block if cursor is not None else None
        if current is None:
            # No legacy vector is presumed attributable at bootstrap.
            current = max(1, finalized - 1)
            block_hash = await substrate.get_block_hash(current)
            async with self.sessions() as session, session.begin():
                await advance_source_emission_cursor(
                    session,
                    netuid=self.netuid,
                    block=current,
                    block_hash=block_hash,
                    expected_block=None,
                    expected_block_hash=None,
                    now=datetime.now(UTC),
                )
        backlog = finalized - current
        catchup = backlog > _SWEEP_CATCHUP_MIN_BACKLOG
        self._backlog_deep = catchup
        if budget is not None:
            # The budget was chosen before the backlog was measured, so a sweep
            # that just discovered a deep backlog must not inherit the
            # steady-state deadline for its first wide batch.
            budget.reschedule(
                asyncio.get_running_loop().time() + self._sweep_timeout_seconds()
            )
        if not catchup:
            # Durable payouts must not wait for a full scan batch to succeed.
            # Keep archive verification and provider fallback on the same path.
            await self.resolve_pending_payouts(substrate)
        batch = _SWEEP_CATCHUP_BATCH_BLOCKS if catchup else _SWEEP_BATCH_BLOCKS
        reads = SourceEmissionReadCache()
        try:
            replayed = catchup and await self._sweep_sparse(
                substrate,
                current=current,
                end=min(finalized, current + _SWEEP_SPARSE_BATCH_BLOCKS),
                finalized=finalized,
                reads=reads,
            )
            if not replayed:
                for block in range(current + 1, min(finalized, current + batch) + 1):
                    await self._scan_block(substrate, block, finalized, reads)
        except Exception:
            # A failed scan must not starve payout resolution: the resolver
            # cycles pending rows by last_checked_at with its own archive
            # reads, so it can make progress when the block scan cannot.
            # A resolver failure is logged, not raised: replacing the scan's
            # error would hide why the cursor stopped (an unaudited runtime
            # once read as a decoder error for weeks).
            try:
                await self.resolve_pending_payouts(substrate)
            except Exception:
                logger.exception("source emission payout resolution failed")
            raise
        await self.resolve_pending_payouts(substrate)
        # Last, and only with budget to spare: payouts and the cursor never
        # wait on the optional comparison.
        await self._run_pending_shadow(substrate, budget)

    async def _run_pending_shadow(
        self, substrate: Any, budget: asyncio.Timeout | None
    ) -> None:
        pending, self._pending_shadow = self._pending_shadow, None
        if pending is None:
            return
        seconds = _REVEAL_PLAN_SHADOW_SECONDS
        deadline = budget.when() if budget is not None else None
        if deadline is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            seconds = min(seconds, remaining - _REVEAL_PLAN_SHADOW_RESERVE_SECONDS)
        if seconds < _REVEAL_PLAN_SHADOW_MIN_SECONDS:
            logger.info("reveal plan shadow skipped: sweep budget exhausted")
            return
        first_block, last_block, acted = pending
        await self._shadow_reveal_plan(
            substrate,
            first_block=first_block,
            last_block=last_block,
            acted=acted,
            seconds=seconds,
        )

    async def _sweep_sparse(
        self,
        substrate: Any,
        *,
        current: int,
        end: int,
        finalized: int,
        reads: SourceEmissionReadCache,
    ) -> bool:
        """Replay ``(current, end]`` reading only blocks that can matter.

        Returns False when no range plan is available (unsupported provider,
        transient read failure, or a structural change inside the range); the
        caller then runs the unchanged per-block scan. Quiet blocks commit as
        one cursor skip; every other block goes through ``_scan_block``.
        """
        if end <= current:
            return False
        planner = (
            plan_source_emission_range_by_reveals
            if _REVEAL_PLAN_MODE == "primary"
            else plan_source_emission_range
        )
        try:
            plan = await planner(
                substrate,
                netuid=self.netuid,
                first_block=current + 1,
                last_block=end,
            )
        except Exception as error:
            chain = getattr(self.state, "chain", None)
            logger.warning(
                "source emission sparse plan unavailable; scanning per block: %s",
                chain._safe_rpc_error(error)[:200]
                if chain is not None
                else type(error).__name__,
            )
            return False
        if plan.full_scan:
            return False
        async with self.sessions() as session:
            cursor = await session.get(SourceEmissionCollectorCursor, self.netuid)
        if (
            cursor is None
            or cursor.block != current
            or cursor.block_hash != plan.hashes[current]
        ):
            raise ValueError("durable cursor does not match the planned range")
        interesting = set(plan.interesting)
        quiet = [b for b in range(current + 1, end + 1) if b not in interesting]
        # Continuous self-audit: the full reader must agree a sampled skip is quiet.
        for block in random.sample(quiet, min(_SPARSE_AUDIT_SAMPLES, len(quiet))):
            observed = await read_source_emission_block(
                substrate,
                netuid=self.netuid,
                block=block,
                finalized_block=finalized,
                cache=SourceEmissionReadCache(),
            )
            if (
                observed.is_payout
                or observed.updates
                or observed.reset_reason
                or observed.block_hash != plan.hashes[block]
            ):
                raise ValueError(f"sparse replay audit disagreed at block {block}")
        cursor_block, cursor_hash = current, plan.hashes[current]
        # Blocks the full reader acted on: what any range plan must include.
        acted: list[int] = []
        stops: list[int | None] = [*sorted(interesting), None]
        for stop in stops:
            target = end if stop is None else stop - 1
            if target > cursor_block:
                async with self.sessions() as session, session.begin():
                    if not await skip_quiet_source_emission_blocks(
                        session,
                        netuid=self.netuid,
                        block=target,
                        block_hash=plan.hashes[target],
                        expected_block=cursor_block,
                        expected_block_hash=cursor_hash,
                        runtime_code_hash=plan.runtime_code_hash,
                        now=datetime.now(UTC),
                    ):
                        raise ValueError("durable cursor moved during sparse replay")
                cursor_block, cursor_hash = target, plan.hashes[target]
            if stop is None:
                break
            observed = await self._scan_block(substrate, stop, finalized, reads)
            if (
                observed.is_payout
                or observed.updates
                or observed.reset_reason
                or observed.invalidated_hotkeys
            ):
                acted.append(stop)
            cursor_block, cursor_hash = stop, plan.hashes[stop]
        if _REVEAL_PLAN_MODE == "shadow":
            if self._pending_shadow is not None:
                # The previous sweep failed before its comparison ran.
                logger.warning(
                    "reveal plan shadow for %s-%s dropped before comparison",
                    *self._pending_shadow[:2],
                )
            self._pending_shadow = (current + 1, end, acted)
        return True

    async def _shadow_reveal_plan(
        self,
        substrate: Any,
        *,
        first_block: int,
        last_block: int,
        acted: list[int],
        seconds: float = 90.0,
    ) -> None:
        """Check the reveal-anchored planner against an already-replayed range.

        Every block the full reader acted on (payouts, weight writes, resets)
        must be in its plan. Extra blocks only cost reads. Never raises: the
        cursor already moved on the event planner's evidence.
        """
        counted = _CallCounter(substrate)
        outcome, missed, detail = "error", [], None
        try:
            async with asyncio.timeout(seconds):
                plan = await plan_source_emission_range_by_reveals(
                    counted,
                    netuid=self.netuid,
                    first_block=first_block,
                    last_block=last_block,
                )
            if plan.full_scan:
                outcome = "fallback"
            else:
                missed = sorted(set(acted) - set(plan.interesting))
                outcome = "missed" if missed else "agreed"
        except Exception as error:
            chain = getattr(self.state, "chain", None)
            detail = (
                chain._safe_rpc_error(error)
                if chain is not None
                else type(error).__name__
            )
        if outcome == "missed":
            logger.warning(
                "reveal plan shadow missed blocks %s in %s-%s",
                missed,
                first_block,
                last_block,
            )
        try:
            async with self.sessions() as session, session.begin():
                await record_reveal_plan_shadow(
                    session,
                    netuid=self.netuid,
                    first_block=first_block,
                    last_block=last_block,
                    outcome=outcome,
                    missed=missed,
                    calls=counted.calls,
                    detail=detail,
                    now=datetime.now(UTC),
                )
        except Exception:
            logger.exception("reveal plan shadow summary write failed")

    async def _scan_block(
        self,
        substrate: Any,
        block: int,
        finalized: int,
        reads: SourceEmissionReadCache,
    ) -> SourceEmissionBlock:
        """Read one finalized block in full and commit it to the cursor."""
        observed = await read_source_emission_block(
            substrate,
            netuid=self.netuid,
            block=block,
            finalized_block=finalized,
            cache=reads,
        )
        payout = None
        payout_blocked_reason = None
        if (
            observed.is_payout
            and (not observed.updates or observed.payout_initialization_reveals)
            and not observed.reset_reason
        ):
            from ditto.chain.errors import ChainEmissionReceiptUnavailable

            try:
                payout = await self._confirmed_receipt(
                    substrate,
                    block=block,
                    allow_initialization_reveals=observed.payout_initialization_reveals,
                )
            except ChainEmissionReceiptUnavailable as error:
                payout_blocked_reason = f"unverifiable_payout: {str(error)[:200]}"
        await self.process_block(
            observed, payout, payout_blocked_reason=payout_blocked_reason
        )
        return observed

    async def _confirmed_receipt(
        self, substrate: Any, *, block: int, allow_initialization_reveals: bool
    ) -> ChainMinerEmissionReceipt:
        """Read a payout receipt, requiring a terminal ambiguity to reproduce.

        Chain state at a finalized block is immutable, so a genuine ambiguity
        reads identically twice. Rate-limited archives have returned one-off
        inconsistent reads that looked like an ownership change, permanently
        discarding real payouts. A second clean read is accepted; a different
        verdict is transport noise and stays retryable.
        """
        from ditto.chain.errors import ChainEmissionReceiptUnavailable

        verdicts: list[str] = []
        for _ in range(2):
            try:
                return await self.state.chain.get_miner_emission_receipt(
                    self.netuid,
                    payout_block=block,
                    target_hotkeys=frozenset(),
                    allow_initialization_reveals=allow_initialization_reveals,
                    substrate=substrate,
                )
            except ChainEmissionReceiptUnavailable as error:
                if verdicts and verdicts[0] == str(error):
                    raise
                verdicts.append(str(error))
        raise ValueError(
            f"payout {block} ambiguity did not reproduce: " + " / ".join(verdicts)
        )

    async def _refetch_receipt(
        self, substrate: Any, *, block: int, block_hash: str, snapshot: dict
    ) -> ChainMinerEmissionReceipt | None:
        """Re-read a reopened payout recorded without a receipt.

        Returns the receipt after persisting it, or None when the row must wait
        (transient failure) or was settled terminally here.
        """
        from ditto.chain.errors import ChainEmissionReceiptUnavailable

        # The scan applied same-block reveals with this marker before snapshotting.
        allow = any(
            binding["block"] == block
            and binding["evidence"].get("payout_initialization_reveal")
            for binding in snapshot["bindings"]
        )
        terminal_reason: str | None = None
        receipt: ChainMinerEmissionReceipt | None = None
        try:
            receipt = await self._confirmed_receipt(
                substrate, block=block, allow_initialization_reveals=allow
            )
        except ChainEmissionReceiptUnavailable as error:
            terminal_reason = f"unverifiable_payout: {str(error)[:200]}"
        except Exception as error:
            retry_reason = "receipt_retry: " + self.state.chain._safe_rpc_error(error)
            async with self.sessions() as session, session.begin():
                await session.execute(
                    self._pending_payout_update(block_hash).values(
                        last_checked_at=datetime.now(UTC),
                        blocked_reason=retry_reason[:300],
                    )
                )
            return None
        if receipt is not None and (
            receipt.block != block
            or receipt.block_hash != block_hash
            or receipt.netuid != self.netuid
        ):
            terminal_reason = "payout receipt does not match finalized block"
        if terminal_reason is None and not any(
            binding["evidence"].get("commit_ciphertext_hash")
            and snapshot["vector_digests"].get(binding["validator_hotkey"])
            == binding["vector_digest"]
            for binding in snapshot["bindings"]
        ):
            # The scan skips this check once a receipt is unavailable.
            terminal_reason = "no_verified_reveal_provenance"
        async with self.sessions() as session, session.begin():
            values: dict[str, Any] = {"last_checked_at": datetime.now(UTC)}
            if receipt is not None:
                values["proof"] = {**snapshot, "receipt": asdict(receipt)}
            if terminal_reason is not None:
                values |= {"terminal": True, "blocked_reason": terminal_reason}
            updated = await session.execute(
                self._pending_payout_update(block_hash)
                .values(**values)
                .returning(SourceEmissionPayout.block_hash)
            )
            if updated.scalar_one_or_none() is None:
                # Another collector settled this payout during the read.
                return None
        return None if terminal_reason is not None else receipt

    def _pending_payout_update(self, block_hash: str) -> Any:
        """Update one payout only while it is still pending and unresolved."""
        return update(SourceEmissionPayout).where(
            SourceEmissionPayout.netuid == self.netuid,
            SourceEmissionPayout.block_hash == block_hash,
            SourceEmissionPayout.terminal.is_(False),
            ~select(SourceEmissionPayoutResolution.netuid)
            .where(
                SourceEmissionPayoutResolution.netuid == SourceEmissionPayout.netuid,
                SourceEmissionPayoutResolution.block_hash
                == SourceEmissionPayout.block_hash,
            )
            .exists(),
        )

    async def process_block(
        self,
        observed: SourceEmissionBlock,
        payout: ChainMinerEmissionReceipt | None,
        *,
        payout_blocked_reason: str | None = None,
    ) -> None:
        """Commit cursor, every vector invalidation and payout snapshot atomically."""
        now = datetime.now(UTC)
        async with self.sessions() as session, session.begin():
            cursor = await session.scalar(
                select(SourceEmissionCollectorCursor)
                .where(
                    SourceEmissionCollectorCursor.netuid == self.netuid,
                )
                .with_for_update()
            )
            if cursor is None:
                raise ValueError("collector cursor is not initialized")
            if cursor.block >= observed.block:
                return
            if (
                cursor.block + 1 != observed.block
                or cursor.block_hash != observed.parent_hash
            ):
                raise ValueError("finalized block does not continue the durable cursor")

            async def apply_updates() -> None:
                for change in observed.updates:
                    await upsert_source_emission_vector_binding(
                        session,
                        netuid=self.netuid,
                        validator_hotkey=change.validator_hotkey,
                        receipt_digest=None,
                        block=observed.block,
                        reveal_block_hash=observed.block_hash,
                        vector_digest=change.vector_digest,
                        evidence={
                            "commit_ciphertext_hash": change.commit_ciphertext_hash,
                            "commit_block": change.commit_block,
                            "reveal_round": change.reveal_round,
                            "payout_initialization_reveal": (
                                observed.payout_initialization_reveals
                            ),
                        },
                    )

            before_payout = (
                observed.is_payout
                and observed.payout_initialization_reveals
                and not observed.reset_reason
            )
            if before_payout:
                await apply_updates()
            # Read only after the upserts so SQLAlchemy cannot reuse the old
            # identity-map values for an equal-vector new submission.
            bindings = await list_source_emission_vector_bindings(
                session, netuid=self.netuid
            )
            if observed.is_payout:
                terminal_reason = (
                    payout_blocked_reason
                    or observed.reset_reason
                    or (
                        "payout_has_vector_writes"
                        if observed.updates and not before_payout
                        else None
                    )
                )
                if payout is None and terminal_reason is None:
                    raise ValueError("payout receipt unavailable")
                if payout is not None and (
                    payout.block != observed.block
                    or payout.block_hash != observed.block_hash
                    or payout.netuid != self.netuid
                ):
                    raise ValueError("payout receipt does not match finalized block")
                if terminal_reason is None and not any(
                    row.evidence.get("commit_ciphertext_hash")
                    and dict(observed.vector_digests).get(row.validator_hotkey)
                    == row.vector_digest
                    for row in bindings
                ):
                    terminal_reason = "no_verified_reveal_provenance"
                snapshot = {
                    "runtime_code_hash": observed.runtime_code_hash,
                    "receipt": asdict(payout) if payout is not None else None,
                    "stake": [asdict(item) for item in observed.voting_stake],
                    "vector_digests": dict(observed.vector_digests),
                    "bindings": [
                        {
                            "validator_hotkey": row.validator_hotkey,
                            "vector_digest": row.vector_digest,
                            "block": row.block,
                            "reveal_block_hash": row.reveal_block_hash,
                            "evidence": row.evidence,
                        }
                        for row in bindings
                    ],
                }
                await record_source_emission_payout(
                    session,
                    netuid=self.netuid,
                    block=observed.block,
                    block_hash=observed.block_hash,
                    proof=snapshot,
                    now=now,
                )
                if terminal_reason:
                    await session.execute(
                        update(SourceEmissionPayout)
                        .where(
                            SourceEmissionPayout.netuid == self.netuid,
                            SourceEmissionPayout.block_hash == observed.block_hash,
                        )
                        .values(terminal=True, blocked_reason=terminal_reason)
                    )
            if observed.reset_reason:
                for row in bindings:
                    if (
                        observed.reset_reason == "uid_mapping_changed"
                        and row.validator_hotkey not in observed.invalidated_hotkeys
                    ):
                        continue
                    row.receipt_digest = None
                    row.vector_digest = None
                    row.block = observed.block
                    row.reveal_block_hash = observed.block_hash
                    row.evidence = {"blocked_reason": observed.reset_reason}
            elif not before_payout:
                await apply_updates()
            cursor.block = observed.block
            cursor.block_hash = observed.block_hash
            cursor.updated_at = now
            cursor.runtime_code_hash = observed.runtime_code_hash
            cursor.last_blocked_reason = observed.reset_reason

    async def _resolve_backing(
        self, substrate: Any, binding: dict, payout: ChainMinerEmissionReceipt
    ) -> tuple[WinnerBacking, FinalizedWeightReceipt] | None:
        evidence = binding["evidence"]
        ciphertext_hash = evidence.get("commit_ciphertext_hash")
        if not ciphertext_hash:
            return None
        ciphertext_hash = ciphertext_hash.removeprefix("0x")
        async with self.sessions() as session:
            rows = await get_finalized_weight_receipts(
                session,
                netuid=self.netuid,
                validator_hotkey=binding["validator_hotkey"],
                ciphertext_hash=ciphertext_hash,
            )
            if len(rows) != 1:
                return None
            row = rows[0]
            receipt = FinalizedWeightReceipt.model_validate(row.receipt)
            digest = row.receipt_digest
            pin_row = await session.get(
                LedgerEpochSnapshot, receipt.provenance.ledger_snapshot_id
            )
            if pin_row is None:
                return None
            pin = LedgerPin.from_row(pin_row)
            expected = pin_expected_shares(pin_row)
        attempt = receipt.attempt
        if (
            attempt.commit_block != evidence.get("commit_block")
            or attempt.reveal_round != evidence.get("reveal_round")
            or vector_digest(attempt.normalized_weights) != binding["vector_digest"]
            or attempt.commit_block >= binding["block"]
            or binding["block"] > payout.block
            or (
                binding["block"] == payout.block
                and not (
                    evidence.get("payout_initialization_reveal") is True
                    and binding["reveal_block_hash"] == payout.block_hash
                )
            )
            or pin.champion_agent_id != receipt.provenance.champion_agent_id
        ):
            return None
        # Verify inclusion separately from authenticated self-report.
        if digest not in self._verified_commits:
            from ditto.chain.source_emission_verifier import (
                verify_finalized_weight_commit,
            )

            try:
                await verify_finalized_weight_commit(substrate, receipt=receipt)
            except ValueError:
                # A disproven claim disqualifies only this validator. Missing
                # historical proof raises ChainConnectionError instead and must
                # escape to sweep's bounded archive-provider fallback.
                return None
            if len(self._verified_commits) >= 4096:
                self._verified_commits.clear()
            self._verified_commits.add(digest)
        consumed = next(
            (
                item
                for item in payout.vectors
                if item.validator_hotkey == receipt.validator_hotkey
            ),
            None,
        )
        if consumed is None:
            return None
        actual: dict[str, int | float] = {
            item.hotkey: item.value for item in consumed.weights
        }
        if receipt.schema_version == 2:
            prescribed = pin_expected_treasury_vector(
                pin, burn_hotkey=payout.owner_hotkey
            )
            for weights in (receipt.weights, actual):
                total = sum(weights.values())
                if total <= 0 or any(
                    abs(weights.get(hotkey, 0) / total - prescribed.get(hotkey, 0))
                    > 0.002
                    for hotkey in set(weights) | set(prescribed)
                ):
                    return None
        burn = pin.context.get("served", {}).get("burn_share")
        if receipt.schema_version == 1 and (
            not isinstance(burn, (int, float)) or not 0 <= burn < 1
        ):
            return None
        for weights in () if receipt.schema_version == 2 else (receipt.weights, actual):
            if (
                classify_vector_against_pins(
                    weights,
                    expected_current=expected,
                    expected_previous=None,
                    burn_hotkey=payout.owner_hotkey,
                )
                != "current"
            ):
                return None
            total = sum(weights.values())
            if (
                total <= 0
                or abs(
                    weights.get(payout.owner_hotkey, 0) / total
                    - pin_expected_burn(burn, expected)
                )
                > 0.002
            ):
                return None
        entry = next(
            (
                e
                for e in pin.entries
                if e.agent_id == receipt.provenance.champion_agent_id
            ),
            None,
        )
        if entry is None or entry.sha256 != receipt.provenance.champion_artifact_sha256:
            return None
        return WinnerBacking(
            receipt.validator_hotkey,
            entry.agent_id,
            entry.sha256,
            entry.miner_hotkey,
            digest,
            binding["vector_digest"],
        ), receipt

    async def resolve_pending_payouts(self, substrate: Any) -> int:
        if not self.confirmation_enabled:
            return 0
        async with self.sessions() as session:
            rows = list(
                await session.scalars(
                    select(SourceEmissionPayout)
                    .where(
                        SourceEmissionPayout.netuid == self.netuid,
                        SourceEmissionPayout.terminal.is_(False),
                        ~select(SourceEmissionPayoutResolution.netuid)
                        .where(
                            SourceEmissionPayoutResolution.netuid
                            == SourceEmissionPayout.netuid,
                            SourceEmissionPayoutResolution.block_hash
                            == SourceEmissionPayout.block_hash,
                        )
                        .exists(),
                    )
                    .order_by(
                        SourceEmissionPayout.last_checked_at.asc().nullsfirst(),
                        SourceEmissionPayout.block,
                    )
                    .limit(100)
                )
            )
            snapshots = [(row.block, row.block_hash, dict(row.proof)) for row in rows]
        confirmed = 0
        refetched = 0
        for block, block_hash, snapshot in snapshots:
            if snapshot.get("receipt") is None:
                # Reopened after a non-reproducible ambiguity verdict.
                if refetched >= _RECEIPT_REFETCH_LIMIT:
                    continue
                refetched += 1
                receipt = await self._refetch_receipt(
                    substrate, block=block, block_hash=block_hash, snapshot=snapshot
                )
                if receipt is None:
                    continue
                snapshot = {**snapshot, "receipt": asdict(receipt)}
            payout = _receipt_from_json(snapshot["receipt"])
            resolved = []
            for binding in snapshot["bindings"]:
                if (
                    snapshot["vector_digests"].get(binding["validator_hotkey"])
                    != binding["vector_digest"]
                ):
                    continue
                backing = await self._resolve_backing(substrate, binding, payout)
                if backing is not None:
                    resolved.append(backing)
            decision = evaluate_winner_payout(
                receipt=payout,
                stake=tuple(VotingStake(**item) for item in snapshot["stake"]),
                backings=tuple(item[0] for item in resolved),
            )
            now = datetime.now(UTC)
            async with self.sessions() as session, session.begin():
                await session.execute(
                    update(SourceEmissionPayout)
                    .where(
                        SourceEmissionPayout.netuid == self.netuid,
                        SourceEmissionPayout.block_hash == block_hash,
                    )
                    .values(last_checked_at=now, blocked_reason=decision.blocked_reason)
                )
                if decision.agent_id is None:
                    continue
                agent = await session.get(Agent, decision.agent_id)
                if (
                    agent is None
                    or agent.sha256 != decision.artifact_sha256
                    or agent.miner_hotkey != decision.miner_hotkey
                ):
                    continue
                proof = {
                    "decision": asdict(decision),
                    "validators": [asdict(item[0]) for item in resolved],
                }
                # JSON carries UUIDs as strings, never mutable ORM objects.
                import json

                proof = json.loads(json.dumps(proof, default=str))
                inserted = await session.scalar(
                    insert(SourceEmissionPayoutResolution)
                    .values(
                        netuid=self.netuid,
                        block_hash=block_hash,
                        agent_id=decision.agent_id,
                        artifact_sha256=decision.artifact_sha256,
                        proof=proof,
                        created_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["netuid", "block_hash"])
                    .returning(SourceEmissionPayoutResolution.agent_id)
                )
                if inserted is None:
                    continue
                supporting = [
                    item for item in resolved if item[0].agent_id == decision.agent_id
                ]
                anchored = supporting[0][1].provenance
                payout_at = datetime.fromtimestamp(payout.block_timestamp, UTC)
                await session.execute(
                    insert(AgentKingship)
                    .values(
                        agent_id=decision.agent_id,
                        first_crowned_at=payout_at,
                    )
                    .on_conflict_do_update(
                        index_elements=["agent_id"],
                        set_={
                            "first_crowned_at": func.least(
                                AgentKingship.first_crowned_at, payout_at
                            ),
                        },
                    )
                )
                await record_emission_confirmed(
                    session,
                    agent_id=decision.agent_id,
                    proof=KingEmissionProof(
                        payout_at,
                        block,
                        block_hash,
                        payout.epoch_index,
                        anchored.ledger_digest,
                        {
                            **proof,
                            "version": 2,
                            "runtime_code_hash": snapshot["runtime_code_hash"],
                            "artifact_sha256": decision.artifact_sha256,
                        },
                    ),
                )
                confirmed += 1
        return confirmed
