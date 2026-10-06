"""SDK startup uses the same fresh finalized snapshot as treasury reads."""

import asyncio

import pytest

from ditto.chain.client import _treasury_substrate


@pytest.mark.asyncio
async def test_startup_metadata_matches_snapshot_without_caching_authorization(
    monkeypatch,
):
    import async_substrate_interface

    heads = iter(("finalized-one", "finalized-two"))
    calls = []

    class SDK:
        def __init__(self, *, url):
            assert url == "ws://synthetic.invalid"
            self.metadata_loads = 0
            self.permission_reads = 0

        async def get_chain_head(self):
            raise AssertionError("treasury startup must not use the best head")

        async def get_chain_finalised_head(self):
            head = next(heads)
            calls.append(("finalized", head))
            return head

        async def __aenter__(self):
            self.startup_block_hash = await self.get_chain_head()
            self.startup_runtime_task = asyncio.create_task(self.load_metadata())
            return self

        async def load_metadata(self):
            self.metadata_loads += 1
            await asyncio.sleep(0)

        async def query(self, *, block_hash):
            # Model the SDK's matching-hash startup join. A different hash
            # would load metadata again instead of joining that task.
            if block_hash == self.startup_block_hash:
                await self.startup_runtime_task
            else:
                await self.load_metadata()
            self.permission_reads += 1
            return self.permission_reads

        async def __aexit__(self, *exc):
            await self.startup_runtime_task
            calls.append(("closed", self.startup_block_hash))

    monkeypatch.setattr(async_substrate_interface, "AsyncSubstrateInterface", SDK)
    for expected in ("finalized-one", "finalized-two"):
        async with _treasury_substrate("ws://synthetic.invalid") as reader:
            assert await reader.get_chain_finalised_head() == expected
            assert await reader.get_chain_head() == expected
            assert await reader.query(block_hash=expected) == 1
            assert await reader.query(block_hash=expected) == 2
            assert reader.metadata_loads == 1
    assert calls == [
        ("finalized", "finalized-one"),
        ("closed", "finalized-one"),
        ("finalized", "finalized-two"),
        ("closed", "finalized-two"),
    ]


@pytest.mark.asyncio
async def test_failed_finalized_head_is_not_retained(monkeypatch):
    import async_substrate_interface

    reads = 0

    class SDK:
        def __init__(self, **kwargs):
            pass

        async def get_chain_finalised_head(self):
            nonlocal reads
            reads += 1
            if reads == 1:
                raise ConnectionError("synthetic failure")
            return "finalized"

    monkeypatch.setattr(async_substrate_interface, "AsyncSubstrateInterface", SDK)
    reader = _treasury_substrate("ws://synthetic.invalid")
    with pytest.raises(ConnectionError):
        await reader.get_chain_head()
    assert await reader.get_chain_head() == "finalized"
    assert reads == 2


@pytest.mark.asyncio
async def test_snapshot_keeps_sdk_cleanup_on_cancelled_metadata(monkeypatch):
    import async_substrate_interface

    cancelled = asyncio.Event()

    class SDK:
        def __init__(self, **kwargs):
            pass

        async def get_chain_finalised_head(self):
            return "finalized"

        async def __aenter__(self):
            await self.get_chain_head()
            self.startup_runtime_task = asyncio.create_task(self.load_metadata())
            return self

        async def load_metadata(self):
            try:
                await asyncio.sleep(100)
            finally:
                cancelled.set()

        async def query(self):
            await self.startup_runtime_task

        async def __aexit__(self, *exc):
            self.startup_runtime_task.cancel()
            await asyncio.gather(self.startup_runtime_task, return_exceptions=True)

    monkeypatch.setattr(async_substrate_interface, "AsyncSubstrateInterface", SDK)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            async with _treasury_substrate("ws://synthetic.invalid") as reader:
                await reader.query()
    assert cancelled.is_set()
    assert reader.startup_runtime_task.done()
