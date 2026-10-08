"""Decoded metadata reuse preserves fresh finalized storage and read budgets."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ditto.chain.client import _treasury_substrate
from ditto.chain.treasury_runtime_cache import TreasuryRuntimeCache


@pytest.mark.asyncio
async def test_metadata_reuse_still_reads_fresh_snapshot_version_and_permissions(
    monkeypatch,
):
    import async_substrate_interface

    heads = iter([("one", 1), ("two", 1), ("three", 2)])
    calls = []

    class SDK:
        def __init__(self, *, url):
            assert url == "ws://synthetic"
            self.head, self.version = next(heads)
            self.runtime_cache = SimpleNamespace(add_item=AsyncMock())
            # add_item is synchronous in the actual SDK.
            self.runtime_cache.add_item = lambda **kwargs: calls.append(
                ("local_runtime", self.head, kwargs["runtime_version"])
            )

        async def get_chain_finalised_head(self):
            calls.append(("head", self.head))
            return self.head

        async def get_block_hash(self, block):
            assert block == 0
            calls.append(("genesis", self.head))
            return "genesis"

        async def get_runtime_for_version(self, version, block_hash):
            calls.append(("decode", block_hash, version))
            return SimpleNamespace(runtime_version=version)

        async def query(self):
            at = await self.get_chain_head()
            # Model the SDK's fresh parent-state specVersion resolution.
            calls.append(("version", at, self.version))
            await self.get_runtime_for_version(self.version, at)
            calls.append(("permission", at))
            return at

    monkeypatch.setattr(async_substrate_interface, "AsyncSubstrateInterface", SDK)
    cache = TreasuryRuntimeCache()
    for expected in ("one", "two", "three"):
        reader = _treasury_substrate("ws://synthetic", runtime_cache=cache)
        assert await reader.query() == expected
    assert [call for call in calls if call[0] == "decode"] == [
        ("decode", "one", 1),
        ("decode", "three", 2),
    ]
    for kind in ("head", "genesis", "version", "permission", "local_runtime"):
        assert sum(call[0] == kind for call in calls) == 3


@pytest.mark.asyncio
async def test_metadata_identity_isolation_and_bounded_eviction():
    cache = TreasuryRuntimeCache()
    load = AsyncMock(return_value=SimpleNamespace(runtime_version=1))
    for url, genesis in (("a", "g1"), ("b", "g1"), ("b", "g2")):
        await cache.get(url, genesis, 1, load)
    assert load.await_count == 3
    for version in (2, 3, 4):
        await cache.get(
            "b",
            "g2",
            version,
            AsyncMock(return_value=SimpleNamespace(runtime_version=version)),
        )
    assert len(cache._values) == 4
    await cache.get("a", "g1", 1, load)
    assert load.await_count == 4


@pytest.mark.asyncio
async def test_concurrent_metadata_loads_share_only_completed_success():
    cache = TreasuryRuntimeCache()

    async def decode():
        await asyncio.sleep(0)
        return SimpleNamespace(runtime_version=1)

    load = AsyncMock(side_effect=decode)
    first, second = await asyncio.gather(
        cache.get("a", "g", 1, load), cache.get("a", "g", 1, load)
    )
    assert first is second
    load.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["failure", "cancel", "mismatch"])
async def test_failed_or_cancelled_metadata_does_not_poison_next_read(fault):
    cache = TreasuryRuntimeCache()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def decode():
        if fault == "failure":
            raise ConnectionError("synthetic unavailable")
        if fault == "mismatch":
            return SimpleNamespace(runtime_version=2)
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    if fault == "cancel":
        task = asyncio.create_task(cache.get("a", "g", 1, decode))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
    else:
        with pytest.raises(ConnectionError if fault == "failure" else ValueError):
            await cache.get("a", "g", 1, decode)
    load = AsyncMock(return_value=SimpleNamespace(runtime_version=1))
    assert (await cache.get("a", "g", 1, load)).runtime_version == 1
    load.assert_awaited_once()
