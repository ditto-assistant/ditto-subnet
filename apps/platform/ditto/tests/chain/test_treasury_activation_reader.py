"""Real shared identity/permit readers on one request-local bounded connection."""

import asyncio
import time

import pytest

from ditto.chain.client import ChainClient
from ditto.chain.errors import (
    ChainTreasuryActivationReadError,
    ChainTreasuryReadTimeoutError,
)
from ditto.tests.api_server.test_treasury_weights import pin
from ditto.tests.chain.test_client import make_chain_config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "connection",
        "epoch_storage",
        "identity_cpu",
        "permit_vector",
        "setter_binding",
        "connection_close",
        "owner",
        "reciprocal",
        "empty_permits",
    ],
)
async def test_combined_reader_keeps_hash_checks_all_permissions_and_deadlines(
    monkeypatch, fault
):
    import async_substrate_interface

    p = pin()
    calls = []
    cancelled = []
    windows = []
    timeout = asyncio.timeout

    class Window:
        def __init__(self, seconds):
            assert seconds == 8
            windows.append(seconds)
            self.inner = timeout(0.01)

        async def __aenter__(self):
            await self.inner.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self.inner.__aexit__(*exc)

        def reschedule(self, when):
            assert 7.9 < when - asyncio.get_running_loop().time() <= 8
            windows.append(8)
            self.inner.reschedule(asyncio.get_running_loop().time() + 0.01)

        def when(self):
            return self.inner.when()

    async def stall(step):
        if fault == step:
            try:
                await asyncio.sleep(100)
            finally:
                cancelled.append(step)

    class Reader:
        def __init__(self, **kwargs):
            calls.append(("open", kwargs))

        async def __aenter__(self):
            await stall("connection")
            return self

        async def __aexit__(self, *exc):
            calls.append(("close", None))
            if exc[0] is None:
                await stall("connection_close")

        async def get_chain_finalised_head(self):
            return p.identity.finalized_block_hash

        async def get_block_number(self, at):
            assert at == p.identity.finalized_block_hash
            return p.identity.finalized_block

        async def get_block_hash(self, block):
            return (
                p.identity.genesis_hash
                if block == 0
                else p.identity.finalized_block_hash
            )

        async def query(self, *, module, storage_function, params, block_hash):
            assert module == "SubtensorModule"
            assert block_hash == p.identity.finalized_block_hash
            calls.append((storage_function, params))
            if storage_function == "SubnetEpochIndex":
                await stall("epoch_storage")
                return p.epoch_index
            if storage_function == "ValidatorPermit":
                await stall("permit_vector")
                return [] if fault == "empty_permits" else [False, True]
            if storage_function == "Keys":
                if params[1] == 1:
                    await stall("setter_binding")
                    return p.fleet[0].validator_hotkey
                if fault == "identity_cpu":
                    # Deliberate sync-decode simulation: cancellation has not
                    # run yet when the first window's clock expires.
                    time.sleep(0.02)
                return p.identity.hotkey
            if storage_function == "Uids":
                if params[1] == p.fleet[0].validator_hotkey:
                    return 0 if fault == "reciprocal" else 1
                return 0
            return {
                "LastEpochBlock": p.first_block,
                "Owner": p.identity.subnet_owner_coldkey
                if fault == "owner"
                else p.identity.owner_coldkey,
                "SubnetOwner": p.identity.subnet_owner_coldkey,
            }[storage_function]

    monkeypatch.setattr(async_substrate_interface, "AsyncSubstrateInterface", Reader)
    monkeypatch.setattr("ditto.chain.client.asyncio.timeout", Window)
    chain = ChainClient(make_chain_config())
    if fault == "none":
        # Separate requests always fetch fresh authorization again.
        for _ in range(2):
            observed, keys = await chain.get_treasury_activation_observation(p.policy)
            assert observed.identity == p.identity
            assert keys == (p.fleet[0].validator_hotkey,)
        assert windows == [8, 8, 8, 8]
        assert sum(c[0] == "open" for c in calls) == 2
        assert sum(c[0] == "close" for c in calls) == 2
        assert sum(c[0] == "ValidatorPermit" for c in calls) == 2
        assert cancelled == []
    else:
        with pytest.raises(ChainTreasuryActivationReadError) as refused:
            await chain.get_treasury_activation_observation(p.policy)
        identity_fault = fault in {
            "connection",
            "epoch_storage",
            "identity_cpu",
            "owner",
        }
        assert refused.value.read_stage == (
            "identity" if identity_fault else "setter_roster"
        )
        assert windows == ([8] if identity_fault else [8, 8])
        if fault in {"owner", "reciprocal", "empty_permits"}:
            assert isinstance(refused.value.read_error, ValueError)
            assert cancelled == []
        else:
            assert isinstance(refused.value.read_error, ChainTreasuryReadTimeoutError)
            assert refused.value.read_error.read_step == (
                "uid_binding" if fault == "identity_cpu" else fault
            )
            assert cancelled == ([] if fault == "identity_cpu" else [fault])
        if identity_fault:
            assert all(c[0] != "ValidatorPermit" for c in calls)
        assert str(refused.value) == "bounded treasury activation read failed"
