"""Permit roster includes unreported validators and binds reciprocal identities."""

import asyncio
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


def test_complete_permitted_roster_at_one_observation_hash():
    pin, _, client = context()
    assert asyncio.run(
        read_finalized_weight_setters(
            client, pin.policy, block_hash=pin.identity.finalized_block_hash
        )
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
def test_roster_absence_drift_and_coercion_refuse(field, value):
    pin, values, client = context()
    values[field] = value
    with pytest.raises(ValueError):
        asyncio.run(
            read_finalized_weight_setters(
                client, pin.policy, block_hash=pin.identity.finalized_block_hash
            )
        )


def test_all_thirteen_permits_are_checked_with_at_most_four_reads_in_flight():
    pin, _, _ = context()

    async def scenario():
        keys = ["5" + letter * 47 for letter in "ABCDEFGHJKLMN"]
        active = peak = 0
        calls = []

        async def read(**kwargs):
            nonlocal active, peak
            calls.append(kwargs)
            assert kwargs["block_hash"] == pin.identity.finalized_block_hash
            if kwargs["storage_function"] == "ValidatorPermit":
                return [True] * 13
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0)
                if kwargs["storage_function"] == "Keys":
                    return keys[kwargs["params"][1]]
                return keys.index(kwargs["params"][1])
            finally:
                active -= 1

        result = await read_finalized_weight_setters(
            SimpleNamespace(query=read),
            pin.policy,
            block_hash=pin.identity.finalized_block_hash,
        )
        assert result == tuple(sorted(keys))
        assert len(calls) == 27
        assert peak == 4 and active == 0
        assert {
            c["params"][1] for c in calls if c["storage_function"] == "Keys"
        } == set(range(13))

    asyncio.run(scenario())


def test_bad_last_permit_refuses_entire_roster_not_a_successful_subset():
    pin, _, _ = context()

    async def scenario():
        keys = ["5" + letter * 47 for letter in "ABCDEFGHJKLMN"]

        async def read(**kwargs):
            if kwargs["storage_function"] == "ValidatorPermit":
                return [True] * 13
            if kwargs["storage_function"] == "Keys":
                return keys[kwargs["params"][1]]
            uid = keys.index(kwargs["params"][1])
            return None if uid == 12 else uid

        with pytest.raises(ValueError, match="not reciprocal"):
            await read_finalized_weight_setters(
                SimpleNamespace(query=read),
                pin.policy,
                block_hash=pin.identity.finalized_block_hash,
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["invalid", "timeout"])
def test_failed_batch_drains_in_flight_siblings(failure):
    pin, _, _ = context()

    async def scenario():
        active = 0
        started = asyncio.Event()

        async def read(**kwargs):
            nonlocal active
            if kwargs["storage_function"] == "ValidatorPermit":
                return [True] * 4
            active += 1
            if active == 4:
                started.set()
            try:
                await started.wait()
                if failure == "invalid" and kwargs["params"][1] == 0:
                    raise ValueError("invalid response")
                await asyncio.Event().wait()
            finally:
                active -= 1

        with pytest.raises(ValueError if failure == "invalid" else TimeoutError):
            async with asyncio.timeout(0.05):
                await read_finalized_weight_setters(
                    SimpleNamespace(query=read),
                    pin.policy,
                    block_hash=pin.identity.finalized_block_hash,
                )
        assert active == 0

    asyncio.run(scenario())
