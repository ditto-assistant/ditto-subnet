"""Sparse catch-up replay commits exactly what the per-block scan commits (#2939).

Each scenario is one 720-block catch-up range of a deterministic chain
(``ditto.tests.chain.source_emission_chain``) whose single state model serves
the full reader, both range planners and the payout receipt. The range is
replayed twice from the same durable state, once through ``_sweep_sparse``
and once block by block through ``_scan_block``, and the cursor, every vector
binding and every payout snapshot must come out identical. Every scenario
runs under a pre-v475 runtime and under a Yuma-gated one, whose
``SubnetEpochConsensus`` both read paths check.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, select

from ditto.api_server import source_emission_collector as module
from ditto.chain.source_emission_verifier import (
    SourceEmissionBlock,
    SourceEmissionReadCache,
    plan_source_emission_range,
    plan_source_emission_range_by_reveals,
)
from ditto.db.models import (
    SourceEmissionCollectorCursor,
    SourceEmissionPayout,
    SourceEmissionVectorBinding,
)
from ditto.tests.api_server.endpoints.test_source_emission_collector import (
    _collector,
    _deep_backlog,
)
from ditto.tests.chain.source_emission_chain import (
    CODE,
    GATED_CODE,
    HOTKEY_SWAP_INTERVAL,
    NETUID,
    SubtensorTimeline,
    foreign_payout,
    foreign_registration,
    foreign_reveal,
    hash_of,
    stake_added,
)

pytestmark = pytest.mark.asyncio

# The cursor sits here before the pre-range history is scanned in full; that
# scan writes the bindings every replay starts from.
SETUP = 980
# One catch-up batch: _SWEEP_SPARSE_BATCH_BLOCKS blocks after the cursor.
FIRST, LAST = 1001, 1720
HEAD = 1800

# Vectors over uids 0 (owner), 3 and 4 (miners).
A_990 = ((0, 6553), (3, 45000), (4, 13982))
B_995 = ((0, 6553), (3, 30000), (4, 28982))
A_NEW = ((0, 6553), (3, 50000), (4, 8982))
B_NEW = ((0, 6553), (3, 20000), (4, 38982))
B_LATE = ((0, 6553), (3, 25000), (4, 33982))

RESET = "uid_mapping_changed"


def _history(
    payouts: tuple[int, ...] = (740, 1100, 1460, 1800),
    swap_interval: int = HOTKEY_SWAP_INTERVAL,
) -> SubtensorTimeline:
    """Both validators committed in epoch 101 and revealed before the range."""
    return (
        SubtensorTimeline(payouts=payouts, head=HEAD, swap_interval=swap_interval)
        .commit(700, "validator-a")
        .commit(710, "validator-b")
        .reveal(990, "validator-a", A_990)
        .reveal(995, "validator-b", B_995)
    )


@dataclass(frozen=True)
class Scenario:
    name: str
    timeline: Callable[[], SubtensorTimeline]
    # What the per-block scan must do, so a fake that answers nothing (or a
    # replay that commits nothing) cannot pass:
    # blocks the full reader acted on (payout, write or reset);
    acted: tuple[int, ...]
    # hotkey -> (binding block, commit block, or why provenance was reset);
    bindings: dict[str, tuple[int, int | str]]
    # payout block -> blocked reason (None: pending, with a receipt).
    payouts: dict[int, str | None]
    # The cursor's blocked reason at the end of the range.
    blocked: str | None = None


SCENARIOS = [
    Scenario(
        "quiet-blocks-only",
        # Commits, other subnets' writes, payouts and registrations, and stake
        # moves: nothing the reader acts on for SN118.
        lambda: (
            _history(payouts=(740, 1800))
            .commit(1050, "validator-a")
            .commit(1300, "validator-b")
            .noise(1111, foreign_reveal())
            .noise(1222, stake_added())
            .noise(1333, foreign_payout())
            .noise(1444, foreign_registration())
        ),
        acted=(),
        bindings={"validator-a": (990, 700), "validator-b": (995, 710)},
        payouts={},
    ),
    Scenario(
        "singleton-reveal",
        lambda: (
            _history().commit(1050, "validator-a").reveal(1200, "validator-a", A_NEW)
        ),
        acted=(1100, 1200, 1460),
        bindings={"validator-a": (1200, 1050), "validator-b": (995, 710)},
        payouts={1100: None, 1460: None},
    ),
    Scenario(
        "two-reveals-in-one-block",
        lambda: (
            _history()
            .commit(1050, "validator-a")
            .commit(1060, "validator-b")
            .reveal(1200, "validator-a", A_NEW)
            .reveal(1200, "validator-b", B_NEW)
        ),
        acted=(1100, 1200, 1460),
        bindings={"validator-a": (1200, 1050), "validator-b": (1200, 1060)},
        payouts={1100: None, 1460: None},
    ),
    Scenario(
        "payout-with-initialization-reveals",
        # 1100 pays out on validator-a's attributable same-block reveal. At
        # 1460 validator-b has two pending commits, so its same-block reveal
        # is unattributable and the payout is terminal; the second commit is
        # revealed alone at 1500.
        lambda: (
            _history()
            .commit(1050, "validator-a")
            .reveal(1100, "validator-a", A_NEW)
            .commit(1300, "validator-b")
            .commit(1350, "validator-b")
            .reveal(1460, "validator-b", B_NEW)
            .reveal(1500, "validator-b", B_LATE)
        ),
        acted=(1100, 1460, 1500),
        bindings={"validator-a": (1100, 1050), "validator-b": (1500, 1350)},
        payouts={1100: None, 1460: "payout_has_vector_writes"},
    ),
    Scenario(
        "uid-reregistration",
        # Both validators weight uid 3, so replacing its miner resets both.
        lambda: (
            _history()
            .commit(1050, "validator-a")
            .register(1250, 3, "miner-c")
            .reveal(1300, "validator-a", A_NEW)
        ),
        acted=(1100, 1250, 1300, 1460),
        bindings={"validator-a": (1300, 1050), "validator-b": (1250, RESET)},
        payouts={1100: None, 1460: None},
    ),
    Scenario(
        "two-registrations-of-one-uid",
        # The second registration replaces a hotkey the first one installed;
        # BlockAtRegistration differs at the ends, and bisecting it finds both.
        lambda: (
            _history()
            .register(1200, 3, "miner-c")
            .commit(1250, "validator-a")
            .reveal(1500, "validator-a", A_NEW)
            .register(1600, 3, "miner-d")
        ),
        acted=(1100, 1200, 1460, 1500, 1600),
        bindings={"validator-a": (1600, RESET), "validator-b": (1200, RESET)},
        payouts={1100: None, 1460: "no_verified_reveal_provenance"},
    ),
    Scenario(
        "hotkey-swap",
        lambda: (
            _history()
            .swap(1250, 1, "validator-a2")
            .commit(1300, "validator-a2")
            .reveal(1500, "validator-a2", A_NEW)
        ),
        acted=(1100, 1250, 1460, 1500),
        bindings={
            "validator-a": (1250, RESET),
            "validator-a2": (1500, 1300),
            "validator-b": (995, 710),
        },
        payouts={1100: None, 1460: None},
    ),
    Scenario(
        "registration-then-swap-restore-hotkey",
        # Keys(118, 3) reads miner-a at both ends of the range. validator-b's
        # reveal between them weights uid 3, so the swap back resets it.
        lambda: (
            _history()
            .register(1200, 3, "intruder")
            .commit(1060, "validator-b")
            .reveal(1300, "validator-b", B_NEW)
            .swap(1550, 3, "miner-a")
        ),
        acted=(1100, 1200, 1300, 1460, 1550),
        bindings={"validator-a": (1200, RESET), "validator-b": (1550, RESET)},
        payouts={1100: None, 1460: None},
    ),
    Scenario(
        "swap-then-registration-restore-hotkey",
        # Keys(118, 3) reads miner-a at both ends of the range. The swap
        # resets validator-b before the 1460 payout snapshots its binding.
        lambda: (
            _history()
            .swap(1200, 3, "swapped")
            .commit(1050, "validator-a")
            .reveal(1300, "validator-a", A_NEW)
            .register(1550, 3, "miner-a")
        ),
        acted=(1100, 1200, 1300, 1460, 1550),
        bindings={"validator-a": (1550, RESET), "validator-b": (1550, RESET)},
        payouts={1100: None, 1460: None},
    ),
    Scenario(
        "swap-away-and-back-under-two-coldkeys",
        # The swap cooldown is per coldkey and a coldkey swap carries none
        # over, so miner-a's hotkey leaves and returns inside the range with
        # no registration: Keys and BlockAtRegistration match at both ends.
        lambda: (
            _history()
            .swap(1200, 3, "swapped")
            .coldkey_swap(1300, "miner-a-coldkey", "fresh-coldkey")
            .swap(1550, 3, "miner-a")
        ),
        acted=(1100, 1200, 1460, 1550),
        bindings={"validator-a": (1550, RESET), "validator-b": (1550, RESET)},
        payouts={1100: None, 1460: "no_verified_reveal_provenance"},
    ),
    Scenario(
        "range-boundaries",
        # The first block pays out on a same-block reveal; the last block
        # swaps a miner both validators weight, so the cursor ends blocked.
        lambda: (
            _history(payouts=(740, FIRST, FIRST + 360, LAST + 1))
            .commit(FIRST - 1, "validator-a")
            .reveal(FIRST, "validator-a", A_NEW)
            .swap(LAST, 4, "miner-d")
        ),
        acted=(FIRST, FIRST + 360, LAST),
        bindings={"validator-a": (LAST, RESET), "validator-b": (LAST, RESET)},
        payouts={FIRST: None, FIRST + 360: None},
        blocked=RESET,
    ),
]


@pytest.fixture
def full_reads(monkeypatch) -> list[SourceEmissionBlock]:
    """Every block the collector reads with the full per-block reader."""
    observed: list[SourceEmissionBlock] = []
    read = module.read_source_emission_block

    async def logged(substrate: Any, **kwargs: Any) -> SourceEmissionBlock:
        block = await read(substrate, **kwargs)
        observed.append(block)
        return block

    monkeypatch.setattr(module, "read_source_emission_block", logged)
    return observed


async def _durable_state(session_maker) -> dict[str, Any]:
    """What a replay commits: the cursor, vector bindings and payout snapshots."""
    async with session_maker() as session:
        cursor = await session.get(SourceEmissionCollectorCursor, NETUID)
        bindings = await session.scalars(
            select(SourceEmissionVectorBinding).where(
                SourceEmissionVectorBinding.netuid == NETUID
            )
        )
        payouts = await session.scalars(
            select(SourceEmissionPayout).where(SourceEmissionPayout.netuid == NETUID)
        )
        return {
            "cursor": {
                "block": cursor.block,
                "block_hash": cursor.block_hash,
                "runtime_code_hash": cursor.runtime_code_hash,
                "last_blocked_reason": cursor.last_blocked_reason,
            },
            "bindings": {
                row.validator_hotkey: {
                    "receipt_digest": row.receipt_digest,
                    "block": row.block,
                    "reveal_block_hash": row.reveal_block_hash,
                    "vector_digest": row.vector_digest,
                    "evidence": row.evidence,
                }
                for row in bindings
            },
            "payouts": {
                row.block: {
                    "block_hash": row.block_hash,
                    # Binding rows come back in heap order, which deleting and
                    # re-inserting between the two replays may change; the
                    # resolver reads the list as a set.
                    "proof": {
                        **row.proof,
                        "bindings": sorted(
                            row.proof["bindings"],
                            key=lambda item: item["validator_hotkey"],
                        ),
                    },
                    "terminal": row.terminal,
                    "blocked_reason": row.blocked_reason,
                }
                for row in payouts
            },
        }


async def _replay_setup(collector, chain, session_maker) -> dict[str, Any]:
    """Reset to ``SETUP`` and scan the pre-range history block by block."""
    async with session_maker() as session, session.begin():
        await session.execute(
            delete(SourceEmissionVectorBinding).where(
                SourceEmissionVectorBinding.netuid == NETUID
            )
        )
        await session.execute(
            delete(SourceEmissionPayout).where(SourceEmissionPayout.netuid == NETUID)
        )
        cursor = await session.get(SourceEmissionCollectorCursor, NETUID)
        cursor.block, cursor.block_hash = SETUP, hash_of(SETUP)
        cursor.runtime_code_hash = cursor.last_blocked_reason = None
        cursor.reveal_plan_shadow = None
    reads = SourceEmissionReadCache()
    for block in range(SETUP + 1, FIRST):
        await collector._scan_block(chain, block, HEAD, reads)
    return await _durable_state(session_maker)


def _acted(reads: list[SourceEmissionBlock]) -> tuple[int, ...]:
    return tuple(
        sorted(
            {
                read.block
                for read in reads
                if read.is_payout
                or read.updates
                or read.reset_reason
                or read.invalidated_hotkeys
            }
        )
    )


def _outcome(state: dict[str, Any]) -> tuple[dict, dict]:
    bindings = {
        hotkey: (
            row["block"],
            row["evidence"].get("commit_block")
            or row["evidence"].get("blocked_reason")
            or "unattributed",
        )
        for hotkey, row in state["bindings"].items()
    }
    payouts = {block: row["blocked_reason"] for block, row in state["payouts"].items()}
    return bindings, payouts


@pytest.mark.parametrize("runtime", [CODE, GATED_CODE], ids=["v473", "yuma-gated"])
@pytest.mark.parametrize("mode", ["primary", "shadow"])
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
async def test_sparse_replay_commits_what_the_per_block_scan_commits(
    app, session_maker, monkeypatch, full_reads, scenario, mode, runtime
):
    assert LAST - FIRST + 1 == module._SWEEP_SPARSE_BATCH_BLOCKS
    monkeypatch.setattr(module, "_REVEAL_PLAN_MODE", mode)
    # The audit re-reads the first quiet blocks instead of a random sample.
    monkeypatch.setattr(module.random, "sample", lambda seq, k: list(seq)[:k])
    chain = scenario.timeline().build(runtime)
    app.state.chain = SimpleNamespace(
        get_miner_emission_receipt=chain.get_miner_emission_receipt,
        _safe_rpc_error=str,
    )
    await _deep_backlog(app, session_maker, SETUP)

    # The reference: today's per-block scan over the range.
    collector = _collector(app, session_maker)
    start = await _replay_setup(collector, chain, session_maker)
    full_reads.clear()
    reads = SourceEmissionReadCache()
    for block in range(FIRST, LAST + 1):
        await collector._scan_block(chain, block, HEAD, reads)
    per_block = await _durable_state(session_maker)
    acted = _acted(full_reads)

    # The same range from the same durable state, replayed sparsely.
    collector = _collector(app, session_maker)
    assert await _replay_setup(collector, chain, session_maker) == start
    full_reads.clear()
    assert await collector._sweep_sparse(
        chain,
        current=FIRST - 1,
        end=LAST,
        finalized=HEAD,
        reads=SourceEmissionReadCache(),
    ), "the planner fell back to the per-block scan"
    sparse = await _durable_state(session_maker)

    assert sparse == per_block

    # The scenario happened as designed: not a fake that answers nothing.
    assert start["cursor"] == {
        "block": FIRST - 1,
        "block_hash": hash_of(FIRST - 1),
        "runtime_code_hash": runtime,
        "last_blocked_reason": None,
    }
    assert acted == scenario.acted
    assert _outcome(per_block) == (scenario.bindings, scenario.payouts)
    assert per_block["cursor"]["last_blocked_reason"] == scenario.blocked
    idle = {
        **start,
        "cursor": {**start["cursor"], "block": LAST, "block_hash": hash_of(LAST)},
    }
    assert (per_block == idle) == (not scenario.acted)
    # And the sparse replay skipped the rest: it read in full every block the
    # scan acted on, the audited quiet blocks and, for the event planner, an
    # other subnet's registration.
    sparse_reads = {read.block for read in full_reads}
    assert set(acted) <= sparse_reads
    assert len(sparse_reads - set(acted)) <= module._SPARSE_AUDIT_SAMPLES + 1

    assert (collector._pending_shadow is not None) == (mode == "shadow")
    if mode == "shadow":
        # The reveal planner, shadowed against what the reader acted on.
        await collector._run_pending_shadow(chain, None)
        async with session_maker() as session:
            cursor = await session.get(SourceEmissionCollectorCursor, NETUID)
            assert cursor.reveal_plan_shadow["last_outcome"] == "agreed"


@pytest.mark.parametrize(
    "planner", [plan_source_emission_range, plan_source_emission_range_by_reveals]
)
async def test_a_swap_cooldown_shorter_than_the_range_is_planned_in_full(planner):
    # One coldkey swaps miner-a away and back 350 blocks apart, which a
    # 300-block cooldown allows. Its swap record then names only the second
    # swap, so the range cannot be planned from the records.
    chain = (
        _history(swap_interval=300)
        .swap(1200, 3, "swapped")
        .swap(1550, 3, "miner-a")
        .build()
    )
    assert chain.state(FIRST - 1).keys == chain.state(LAST).keys
    plan = await planner(chain, netuid=NETUID, first_block=FIRST, last_block=LAST)
    assert (plan.full_scan, plan.full_scan_reason) == (True, "hotkey_swap_interval")


@pytest.mark.parametrize(
    "planner", [plan_source_emission_range, plan_source_emission_range_by_reveals]
)
async def test_a_runtime_upgrade_inside_the_range_is_planned_in_full(planner):
    # v473 to a Yuma-gated runtime: the per-block scan alone reads the
    # boundary, where the new runtime's state is first written.
    chain = _history().upgrade(1300, GATED_CODE).build()
    plan = await planner(chain, netuid=NETUID, first_block=FIRST, last_block=LAST)
    assert (plan.full_scan, plan.full_scan_reason) == (True, "runtime_changed")
