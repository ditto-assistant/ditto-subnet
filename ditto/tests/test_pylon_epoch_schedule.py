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


def test_manual_epoch_and_tempo_changes_use_chain_state():
    assert state(pending_epoch_at=9_029_460).next_epoch_block == 9_029_460
    assert state(pending_epoch_at=9_030_000).next_epoch_block == 9_029_509
    assert state(tempo=720).next_epoch_block == 9_029_869


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
