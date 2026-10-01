"""Permit roster includes unreported validators and binds reciprocal identities."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ditto_screening_protocol.treasury_enforcement import EnforcingTreasuryPin
from ditto_screening_protocol.treasury_identity import read_finalized_weight_setters


def context():
    pin = EnforcingTreasuryPin.model_validate_json(
        (Path(__file__).parent / "fixtures/treasury_enforcing_pin_v2.json").read_text()
    )
    values = {
        "ValidatorPermit": [False, True],
        "Keys": pin.fleet[0].validator_hotkey,
        "Uids": 1,
    }

    async def read(**kwargs):
        assert kwargs["block_hash"] == pin.identity.finalized_block_hash
        assert kwargs["module"] == "SubtensorModule"
        return values[kwargs["storage_function"]]

    return pin, values, SimpleNamespace(query=AsyncMock(side_effect=read))


@pytest.mark.asyncio
async def test_complete_permitted_roster_at_one_observation_hash():
    pin, _, client = context()
    assert await read_finalized_weight_setters(
        client, pin.policy, block_hash=pin.identity.finalized_block_hash
    ) == (pin.fleet[0].validator_hotkey,)
    assert client.query.await_count == 3
    calls = client.query.await_args_list
    assert calls[1].kwargs["params"] == [118, 1]
    assert calls[2].kwargs["params"] == [118, pin.fleet[0].validator_hotkey]


@pytest.mark.parametrize(
    "field,value",
    [
        ("Uids", None),
        ("Uids", True),
        ("Uids", 0),
        ("Uids", "1"),
        ("Keys", "invalid"),
        ("ValidatorPermit", []),
        ("ValidatorPermit", [False]),
        ("ValidatorPermit", [0, 1]),
    ],
)
@pytest.mark.asyncio
async def test_roster_absence_drift_and_coercion_refuse(field, value):
    pin, values, client = context()
    values[field] = value
    with pytest.raises(ValueError):
        await read_finalized_weight_setters(
            client, pin.policy, block_hash=pin.identity.finalized_block_hash
        )
