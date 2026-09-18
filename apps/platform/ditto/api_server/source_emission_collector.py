"""Durable finalized-chain attribution; unidentified vectors never reveal source."""

from __future__ import annotations

import asyncio
import logging
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
    pin_expected_shares,
)
from ditto.chain.models import (
    ChainMinerEarning,
    ChainMinerEmissionReceipt,
    ChainWeight,
    ChainWeightVector,
)
from ditto.chain.source_emission_verifier import (
    SourceEmissionBlock,
    VotingStake,
    WinnerBacking,
    evaluate_winner_payout,
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
    record_source_emission_payout,
    upsert_source_emission_vector_binding,
)
from ditto.db.queries.weight_receipts import get_finalized_weight_receipts

logger = logging.getLogger(__name__)


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
                async with asyncio.timeout(120):
                    async with AsyncSubstrateInterface(url=url) as substrate:
                        await self._sweep_provider(substrate)
                return
            except Exception as error:
                failures.append(self.state.chain._safe_rpc_error(error)[:200])
        raise RuntimeError(
            "all source-emission archive providers failed: " + "; ".join(failures)
        )

    async def _sweep_provider(self, substrate: Any) -> None:
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
        for block in range(current + 1, min(finalized, current + 16) + 1):
            observed = await read_source_emission_block(
                substrate, netuid=self.netuid, block=block
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
                    payout = await self.state.chain.get_miner_emission_receipt(
                        self.netuid,
                        payout_block=block,
                        target_hotkeys=frozenset(),
                        allow_initialization_reveals=observed.payout_initialization_reveals,
                        substrate=substrate,
                    )
                except ChainEmissionReceiptUnavailable as error:
                    payout_blocked_reason = f"unverifiable_payout: {str(error)[:200]}"
            await self.process_block(
                observed, payout, payout_blocked_reason=payout_blocked_reason
            )
        await self.resolve_pending_payouts(substrate)

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
        burn = pin.context.get("served", {}).get("burn_share")
        if not isinstance(burn, (int, float)) or not 0 <= burn < 1:
            return None
        for weights in (receipt.weights, actual):
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
                or abs(weights.get(payout.owner_hotkey, 0) / total - burn) > 0.002
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
        for block, block_hash, snapshot in snapshots:
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
