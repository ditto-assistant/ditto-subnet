from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ditto.chain.errors import ChainConnectionError
from ditto.chain.models import ChainWeightsSnapshot
from ditto.chain.weight_diagnostics import (
    PendingWeightCommit,
    WeightDiagnostics,
    counter,
    predict_next_epoch_block,
    read_weight_diagnostics,
    vector,
)
from ditto.tests.chain.test_client import AsyncRows

# Live SN118 head 9032718 (epoch 25025, boundary 9032389): UID 0's legacy
# drand 1.0.1 commit at 9032460 targeted round 32061278, inside the same
# epoch; UID 45's stateful commit at 9032400 targeted round 32062423, which is
# the next boundary 9032749 plus the +3 security offset.
HEAD_TIMESTAMP = 1_788_990_228
LEGACY = ("5" + "A" * 47, 9_032_460, b"ciphertext-must-not-escape", 32_061_278)
STATEFUL = ("5" + "B" * 47, 9_032_400, b"ciphertext-must-not-escape", 32_062_423)


def block_timestamp(block: int) -> int:
    return HEAD_TIMESTAMP - (9_032_718 - block) * 12


async def test_all_diagnostics_use_the_revealed_matrix_hash(install_substrate_module):
    substrate = install_substrate_module
    snapshot = ChainWeightsSnapshot(118, 9_032_718, "0x" + "ab" * 32, None, ())
    values = {
        "ValidatorTrust": [65534, 60947],
        "LastUpdate": [9_032_460, 9_032_400],
        "Consensus": [0, 65535],
        "LastEpochBlock": 9_032_389,
        "PendingEpochAt": 0,
        "SubnetEpochIndex": 25_025,
        "Tempo": 360,
        "BlocksSinceLastStep": 329,
    }
    hashes = {b: f"0x{b:064x}" for b in (9_032_400, 9_032_460)}

    async def query(**kwargs):
        if kwargs["module"] == "Timestamp":
            assert kwargs["storage_function"] == "Now" and kwargs["params"] == []
            block = next(b for b, h in hashes.items() if h == kwargs["block_hash"])
            return block_timestamp(block) * 1000
        assert kwargs["block_hash"] == snapshot.block_hash
        assert kwargs["params"] == [118]
        return values[kwargs["storage_function"]]

    substrate.query.side_effect = query
    substrate.get_block_hash = AsyncMock(side_effect=lambda b: hashes[b])
    substrate.query_map.return_value = AsyncRows([(25_025, [LEGACY, STATEFUL])])
    client = SimpleNamespace(
        get_weights=AsyncMock(return_value=snapshot), _substrate_url=lambda: "ws://test"
    )
    result = await read_weight_diagnostics(client, 118)
    assert result.validator_trust == (65534, 60947)
    assert (result.tempo, result.blocks_since_last_step) == (360, 329)
    assert result.next_epoch_block == 9_032_749
    legacy, stateful = result.pending
    assert (legacy.commit_block, legacy.reveal_round) == (9_032_460, 32_061_278)
    assert legacy.commit_block_timestamp == block_timestamp(9_032_460)
    # Legacy lane: a reveal inside the commit's own epoch.
    assert legacy.implied_reveal_block == 9_032_466
    assert result.reveal_offset_blocks(legacy) == 9_032_466 - 9_032_749
    # Stateful lane: next boundary + SECURITY_BLOCK_OFFSET.
    assert stateful.implied_reveal_block == 9_032_752
    assert result.reveal_offset_blocks(stateful) == 3
    assert "ciphertext" not in repr(result)
    assert substrate.query_map.await_args.kwargs["block_hash"] == snapshot.block_hash
    # One timestamp read per distinct commit block, nothing else re-read.
    assert substrate.get_block_hash.await_count == 2


async def test_unreadable_commit_timestamp_degrades_one_row(install_substrate_module):
    substrate = install_substrate_module
    snapshot = ChainWeightsSnapshot(118, 9_032_718, "0x" + "ab" * 32, None, ())
    values = {
        "ValidatorTrust": [65534],
        "LastUpdate": [9_032_400],
        "Consensus": [65535],
        "LastEpochBlock": 9_032_389,
        "PendingEpochAt": 0,
        "SubnetEpochIndex": 25_025,
        "Tempo": 360,
        "BlocksSinceLastStep": 329,
    }

    async def query(**kwargs):
        if kwargs["module"] == "Timestamp":
            raise RuntimeError("state pruned")
        return values[kwargs["storage_function"]]

    substrate.query.side_effect = query
    substrate.get_block_hash = AsyncMock(return_value="0x" + "cd" * 32)
    substrate.query_map.return_value = AsyncRows([(25_024, [STATEFUL])])
    client = SimpleNamespace(
        get_weights=AsyncMock(return_value=snapshot), _substrate_url=lambda: "ws://test"
    )
    result = await read_weight_diagnostics(client, 118)
    (row,) = result.pending
    assert row.commit_block_timestamp is None
    assert row.implied_reveal_block is None
    assert result.reveal_offset_blocks(row) is None


def test_previous_epoch_commit_offset_uses_the_boundary_that_ended_it():
    diagnostics = WeightDiagnostics(
        ChainWeightsSnapshot(118, 9_032_718, "0x" + "ab" * 32, None, ()),
        (65535,),
        (9_032_400,),
        (65535,),
        9_032_389,
        0,
        25_025,
        (),
        360,
        329,
        9_032_749,
    )
    # A commit from epoch 25024 aimed at 9032389 + 3, the boundary that ended it.
    previous = PendingWeightCommit(
        "5" + "B" * 47,
        25_024,
        9_032_040,
        (block_timestamp(9_032_392) - 1_692_803_367) // 3,
        block_timestamp(9_032_040),
    )
    assert previous.implied_reveal_block == 9_032_392
    assert diagnostics.reveal_offset_blocks(previous) == 3
    stale = PendingWeightCommit("5" + "B" * 47, 25_000, 9_020_000, 1, 1_700_000_000)
    assert diagnostics.reveal_offset_blocks(stale) is None


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        # bittensor-drand 2.0.0 predict vectors and the live SN118 head.
        ({"last_epoch_block": 10, "tempo": 50, "current_block": 10}, 60),
        (
            {
                "last_epoch_block": 80,
                "pending_epoch_at": 95,
                "tempo": 20,
                "current_block": 91,
            },
            95,
        ),
        (
            {
                "last_epoch_block": 9_032_389,
                "tempo": 360,
                "blocks_since_last_step": 329,
                "current_block": 9_032_718,
            },
            9_032_749,
        ),
        # Safety net and deferred pending epoch both fire on the next block.
        (
            {
                "last_epoch_block": 100,
                "tempo": 360,
                "blocks_since_last_step": 50_401,
                "current_block": 200,
            },
            201,
        ),
        (
            {
                "last_epoch_block": 100,
                "pending_epoch_at": 150,
                "tempo": 360,
                "blocks_since_last_step": 100,
                "current_block": 200,
            },
            201,
        ),
    ],
)
def test_next_epoch_prediction_matches_drand_simulation(fields, expected):
    arguments = {"pending_epoch_at": 0, "blocks_since_last_step": 0, **fields}
    assert predict_next_epoch_block(**arguments) == expected


def test_next_epoch_prediction_rejects_unusable_schedules():
    with pytest.raises(ValueError):
        predict_next_epoch_block(
            last_epoch_block=10,
            pending_epoch_at=0,
            tempo=0,
            blocks_since_last_step=0,
            current_block=10,
        )
    with pytest.raises(ValueError):
        predict_next_epoch_block(
            last_epoch_block=11,
            pending_epoch_at=0,
            tempo=1,
            blocks_since_last_step=0,
            current_block=10,
        )


async def test_chain_failure_never_returns_empty_healthy_evidence():
    client = SimpleNamespace(
        get_weights=AsyncMock(side_effect=RuntimeError("unavailable"))
    )
    with pytest.raises(ChainConnectionError, match="diagnostics unavailable"):
        await read_weight_diagnostics(client, 118)


@pytest.mark.parametrize("value", [None, True, -1, 1.0, "1", 2**64])
def test_counters_are_strict(value):
    with pytest.raises(ValueError):
        counter(value)


def test_invalid_trust_and_empty_vectors_are_rejected():
    for values in ([], [65536], [True], None):
        with pytest.raises(ValueError):
            vector(values, 65535)
