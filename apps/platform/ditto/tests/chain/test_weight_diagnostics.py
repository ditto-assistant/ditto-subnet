from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ditto.chain.errors import ChainConnectionError
from ditto.chain.models import ChainWeightsSnapshot
from ditto.chain.weight_diagnostics import counter, read_weight_diagnostics, vector
from ditto.tests.chain.test_client import AsyncRows


async def test_all_diagnostics_use_the_revealed_matrix_hash(install_substrate_module):
    substrate = install_substrate_module
    snapshot = ChainWeightsSnapshot(118, 1000, "0x" + "ab" * 32, None, ())
    values = {
        "ValidatorTrust": [65534, 60947],
        "LastUpdate": [950, 999],
        "Consensus": [0, 65535],
        "LastEpochBlock": 900,
        "PendingEpochAt": 0,
        "SubnetEpochIndex": 42,
    }

    async def query(**kwargs):
        assert kwargs["block_hash"] == snapshot.block_hash
        assert kwargs["params"] == [118]
        return values[kwargs["storage_function"]]

    substrate.query.side_effect = query
    substrate.query_map.return_value = AsyncRows(
        [
            (42, [("5" + "A" * 47, 999, b"ciphertext-must-not-escape", 1234)]),
        ]
    )
    client = SimpleNamespace(
        get_weights=AsyncMock(return_value=snapshot), _substrate_url=lambda: "ws://test"
    )
    result = await read_weight_diagnostics(client, 118)
    assert result.validator_trust == (65534, 60947)
    assert result.pending[0].reveal_round == 1234
    assert result.pending[0].commit_block == 999
    assert "ciphertext" not in repr(result)
    assert substrate.query_map.await_args.kwargs["block_hash"] == snapshot.block_hash


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
