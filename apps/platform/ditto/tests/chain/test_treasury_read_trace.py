"""Bounded chain-read diagnostics preserve the calls, deadline and cancellation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ditto.chain.client import ChainClient
from ditto.chain.errors import ChainTreasuryReadTimeoutError
from ditto.chain.treasury_read_trace import TreasuryReadTrace
from ditto.tests.chain.test_client import make_chain_config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args,kwargs,setters,expected",
    [
        ("get_chain_finalised_head", (), {}, False, "finalized_head"),
        ("get_block_number", ("exact-hash",), {}, False, "finalized_height"),
        ("get_block_hash", (100,), {}, False, "canonical_hash"),
        ("get_block_hash", (0,), {}, False, "genesis_hash"),
        ("query", (), {"storage_function": "SubnetEpochIndex"}, False, "epoch_storage"),
        ("query", (), {"storage_function": "Owner"}, False, "collector_storage"),
        ("query", (), {"storage_function": "Keys"}, False, "uid_binding"),
        ("query", (), {"storage_function": "ValidatorPermit"}, True, "permit_vector"),
        ("query", (), {"storage_function": "Uids"}, True, "setter_binding"),
    ],
)
async def test_trace_forwards_exact_call_without_sanitizing_evidence(
    method, args, kwargs, setters, expected
):
    # An invalid value must reach the authoritative reader for refusal; labels
    # must not manufacture a valid value or inspect addresses/provider params.
    response = object()
    call = AsyncMock(return_value=response)
    trace = TreasuryReadTrace(setters=setters)
    trace.client = SimpleNamespace(**{method: call})
    assert await getattr(trace, method)(*args, **kwargs) is response
    call.assert_awaited_once_with(*args, **kwargs)
    assert trace.step == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("stall", ["connection", "epoch_storage", "connection_close"])
async def test_original_eight_second_bound_reports_checkpoint_and_cancels(
    monkeypatch, stall
):
    import async_substrate_interface

    cancelled = []

    async def block(stage):
        try:
            await asyncio.sleep(100)
        finally:
            cancelled.append(stage)

    class Reader:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            if stall == "connection":
                await block(stall)
            return self

        async def __aexit__(self, *exc):
            if stall == "connection_close":
                await block(stall)

        async def query(self, **_kwargs):
            if stall == "epoch_storage":
                await block(stall)
            return "unmodified-evidence"

    monkeypatch.setattr(async_substrate_interface, "AsyncSubstrateInterface", Reader)
    timeout = asyncio.timeout

    def short_timeout(seconds):
        assert seconds == 8  # Production deadline remains unchanged.
        return timeout(0.01)

    monkeypatch.setattr("ditto.chain.client.asyncio.timeout", short_timeout)
    chain = ChainClient(make_chain_config())
    with pytest.raises(ChainTreasuryReadTimeoutError) as failure:
        async with chain._treasury_reader() as traced:
            await traced.query(
                storage_function="SubnetEpochIndex", block_hash="exact-hash"
            )
    assert failure.value.read_step == stall
    assert str(failure.value) == "bounded treasury read timed out"
    assert cancelled == [stall]
