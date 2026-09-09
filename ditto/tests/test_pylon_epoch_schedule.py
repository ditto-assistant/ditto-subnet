"""Regression for the observed SN118 epoch split, without a wallet or RPC."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "services/pylon" / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


epoch = load("ditto_pylon_epoch")
patcher = load("patch_epoch_schedule")


def state(block=9_029_448, **changes):
    return epoch.EpochSchedule(
        **{
            "last_epoch_block": 9_029_149,
            "pending_epoch_at": 0,
            "subnet_epoch_index": 25_016,
            "tempo": 360,
            "blocks_since_last_step": block - 9_029_149,
            "current_block": block,
            **changes,
        }
    )


def test_live_incident_early_and_late_commits_share_one_stateful_epoch():
    early, late = state(9_029_197), state(9_029_448)
    assert early.next_epoch_block == late.next_epoch_block == 9_029_509

    # The shipped drand 1.0.1 formula selected two different reveal boundaries
    # for these actual commits, although both belong to chain epoch 25016.
    def legacy(block):
        return ((block + 119) // 361 + 1) * 361 - 119 + 3

    assert legacy(early.current_block) == 9_029_216
    assert legacy(late.current_block) == 9_029_577
    assert legacy(late.current_block) - late.next_epoch_block == 68


def test_live_incident_commits_share_one_drand_v2_ingest_block():
    # drand 2.0.0 encrypts for predict_first_reveal_block + SECURITY_BLOCK_OFFSET.
    early, late = state(9_029_197), state(9_029_448)
    assert early.predict_first_reveal_block(1) == late.predict_first_reveal_block(1)
    assert early.target_ingest_block(1) == late.target_ingest_block(1) == 9_029_512
    assert (
        early.target_ingest_block(1)
        == early.next_epoch_block + epoch.SECURITY_BLOCK_OFFSET
    )


def test_manual_epoch_and_tempo_changes_use_chain_state():
    assert state(pending_epoch_at=9_029_460).next_epoch_block == 9_029_460
    assert state(pending_epoch_at=9_030_000).next_epoch_block == 9_029_509
    assert state(tempo=720).next_epoch_block == 9_029_869


@pytest.mark.parametrize(
    ("name", "fields", "reveal_period", "expected"),
    [
        # bittensor-drand 2.0.0 src/epoch_schedule_vectors.rs predict_vectors.
        (
            "cycle_reset",
            {
                "last_epoch_block": 10,
                "tempo": 50,
                "blocks_since_last_step": 0,
                "current_block": 10,
            },
            1,
            60,
        ),
        (
            "pending_fires_before_auto",
            {
                "last_epoch_block": 80,
                "pending_epoch_at": 95,
                "tempo": 20,
                "blocks_since_last_step": 0,
                "current_block": 91,
            },
            1,
            95,
        ),
    ],
)
def test_reveal_prediction_matches_upstream_drand_vectors(
    name, fields, reveal_period, expected
):
    vector = state(**{"subnet_epoch_index": 0, "pending_epoch_at": 0, **fields})
    assert vector.predict_first_reveal_block(reveal_period) == expected, name
    # The Pylon task window fires at the same block drand reveals in.
    assert vector.next_epoch_block == expected, name


def test_commit_epoch_is_taken_at_the_extrinsic_block():
    # bittensor-drand 2.0.0 commit_epoch_vectors: head 120, inclusion at 121.
    vector = state(
        last_epoch_block=100,
        pending_epoch_at=0,
        subnet_epoch_index=0,
        tempo=50,
        blocks_since_last_step=0,
        current_block=120,
    )
    assert vector.current_epoch_pre_run_coinbase(121) == 0
    assert vector.current_epoch_pre_run_coinbase(150) == 1
    assert vector.simulate_run_coinbase(150) == epoch.EpochSchedule(
        last_epoch_block=150,
        pending_epoch_at=0,
        subnet_epoch_index=1,
        tempo=50,
        blocks_since_last_step=0,
        current_block=150,
    )


def test_boundary_head_commit_belongs_to_the_next_epoch():
    # A commit encrypted at head 9029508 is included on the fire block
    # 9029509, whose pre-run_coinbase epoch is already 25017.
    boundary = state(9_029_508)
    assert boundary.current_epoch_pre_run_coinbase(9_029_509) == 25_017
    assert boundary.target_ingest_block(1) == 9_029_869 + 3


def test_simulation_and_two_rule_approximation_disagree_after_deferral():
    # These are the cases where min(last + tempo, pending) is not what the
    # chain does. Pylon's expiry window must follow the chain, not the shortcut.
    def two_rule(s):
        automatic = s.last_epoch_block + s.tempo
        return min(automatic, s.pending_epoch_at) if s.pending_epoch_at else automatic

    # BlocksSinceLastStep safety net: Subtensor steps on the very next block.
    safety = state(
        200, last_epoch_block=100, blocks_since_last_step=epoch.MAX_TEMPO + 1
    )
    assert two_rule(safety) == 460
    assert safety.next_epoch_block == 201

    # A pending epoch whose block already passed (deferred) fires next block;
    # the shortcut would place the boundary in the past.
    deferred = state(
        200, last_epoch_block=100, pending_epoch_at=150, blocks_since_last_step=100
    )
    assert two_rule(deferred) == 150 < deferred.current_block
    assert deferred.next_epoch_block == 201
    # The commit is included on that fire block, so it belongs to the new
    # epoch and reveals at the boundary after it, not at 201 + 3.
    assert deferred.current_epoch_pre_run_coinbase(201) == 25_017
    assert deferred.target_ingest_block(1) == 201 + 360 + 3


def test_simulation_budget_is_bounded():
    with pytest.raises(ValueError, match="reveal period"):
        state().predict_first_reveal_block(-1)
    with pytest.raises(ValueError, match="reveal period"):
        state().predict_first_reveal_block(True)


@pytest.mark.parametrize(
    "change",
    [
        {"tempo": 0},
        {"tempo": 50_401},
        {"tempo": True},
        {"last_epoch_block": 10_000_000},
        {"subnet_epoch_index": None},
        {"pending_epoch_at": -1},
        {"blocks_since_last_step": 1.0},
        {"current_block": 2**64},
    ],
)
def test_malformed_or_unavailable_schedule_never_selects_legacy_fallback(change):
    with pytest.raises(ValueError):
        state(**change)


async def test_schedule_reads_all_fields_at_the_supplied_hash():
    expected = state()
    fields = expected.drand_arguments()
    values = {
        f"SubtensorModule.{storage}": fields[field]
        for field, storage in epoch.SCHEDULE_STORAGE.items()
    }

    async def storage(name, netuid, *, block_hash):
        assert netuid == 118 and block_hash == "0x" + "a" * 64
        return values[name]

    read = AsyncMock(side_effect=storage)
    client = SimpleNamespace(
        subtensor=SimpleNamespace(state=SimpleNamespace(getStorage=read))
    )
    found = await epoch.read_epoch_schedule(
        client, 118, expected.current_block, "0x" + "a" * 64
    )
    assert found == expected
    assert read.await_count == 5
    assert set(found.drand_arguments()) == {*epoch.SCHEDULE_STORAGE, "current_block"}


async def test_partial_schedule_read_fails_closed():
    read = AsyncMock(side_effect=RuntimeError("storage unavailable"))
    client = SimpleNamespace(
        subtensor=SimpleNamespace(state=SimpleNamespace(getStorage=read))
    )
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await epoch.read_epoch_schedule(client, 118, 123, "0x" + "a" * 64)


@pytest.mark.parametrize("name", patcher.SOURCE_HASHES)
def test_build_patch_refuses_changed_dependency_bytes(name):
    with pytest.raises(ValueError, match="unreviewed"):
        patcher.patch(name, "# unexpected upstream release\n")


def test_patch_anchor_is_unique():
    with pytest.raises(ValueError):
        patcher.replace_once("twice twice", "twice", "replacement")


def test_dependency_adaptation_refuses_an_unreviewed_manifest():
    with pytest.raises(ValueError, match="unreviewed"):
        patcher.patch_dependencies("[project]\ndependencies=[]\n")
