"""Receipt provider fallback reports only a fixed final checkpoint."""

import asyncio
from types import SimpleNamespace

import pytest

from ditto.chain.client import ChainClient
from ditto.chain.errors import ChainTreasuryReceiptUnavailable


@pytest.mark.asyncio
async def test_receipt_timeout_checkpoint_and_fallback_without_private_errors(
    monkeypatch,
):
    import async_substrate_interface

    import ditto.chain.treasury_receipts as receipts

    calls = []

    class SDK:
        def __init__(self, *, url):
            calls.append(url)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def proof(*_args, progress, **_kwargs):
        progress.phase = "source_events"
        await asyncio.sleep(1)

    monkeypatch.setattr(async_substrate_interface, "AsyncSubstrateInterface", SDK)
    monkeypatch.setattr(receipts, "read_treasury_chain_proof", proof)
    client = object.__new__(ChainClient)
    client._config = SimpleNamespace(archive_rpc_timeout_seconds=0.01)
    monkeypatch.setattr(
        client, "_historical_substrate_urls", lambda: ("PRIVATE-one", "PRIVATE-two")
    )
    with pytest.raises(ChainTreasuryReceiptUnavailable) as exc:
        await client.get_treasury_receipt_proof(
            None,
            None,
            sender="x",
            recipient="y",
            asset="TAO",
            recipient_hotkey=None,
            pinned_block=1,
            pinned_block_hash="z",
            pinned_uid=0,
        )
    assert exc.value.read_phase == "source_events"
    assert exc.value.attempt_count == 2 and exc.value.timed_out
    assert calls == ["PRIVATE-one", "PRIVATE-two"]
    assert str(exc.value) == "finalized treasury receipt unavailable"


@pytest.mark.asyncio
async def test_receipt_timeout_survives_later_connection_failure(monkeypatch):
    import async_substrate_interface

    calls = []

    class SDK:
        def __init__(self, *, url):
            calls.append(url)

        async def __aenter__(self):
            if len(calls) == 1:
                raise TimeoutError("PRIVATE first provider")
            raise ConnectionError("PRIVATE second provider")

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(async_substrate_interface, "AsyncSubstrateInterface", SDK)
    client = object.__new__(ChainClient)
    client._config = SimpleNamespace(archive_rpc_timeout_seconds=10)
    monkeypatch.setattr(client, "_historical_substrate_urls", lambda: ("one", "two"))
    with pytest.raises(ChainTreasuryReceiptUnavailable) as exc:
        await client.get_treasury_receipt_proof(
            None,
            None,
            sender="x",
            recipient="y",
            asset="TAO",
            recipient_hotkey=None,
            pinned_block=1,
            pinned_block_hash="z",
            pinned_uid=0,
        )
    assert exc.value.attempt_count == 2 and exc.value.timed_out
    assert exc.value.read_phase == "connection"


@pytest.mark.asyncio
async def test_receipt_contradiction_never_falls_back(monkeypatch):
    import async_substrate_interface

    import ditto.chain.treasury_receipts as receipts

    calls = []

    class SDK:
        def __init__(self, *, url):
            calls.append(url)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def proof(*_args, **_kwargs):
        raise ValueError("contradicting exact chain effect")

    monkeypatch.setattr(async_substrate_interface, "AsyncSubstrateInterface", SDK)
    monkeypatch.setattr(receipts, "read_treasury_chain_proof", proof)
    client = object.__new__(ChainClient)
    client._config = SimpleNamespace(archive_rpc_timeout_seconds=10)
    monkeypatch.setattr(client, "_historical_substrate_urls", lambda: ("one", "two"))
    with pytest.raises(ValueError, match="contradicting"):
        await client.get_treasury_receipt_proof(
            None,
            None,
            sender="x",
            recipient="y",
            asset="TAO",
            recipient_hotkey=None,
            pinned_block=1,
            pinned_block_hash="z",
            pinned_uid=0,
        )
    assert calls == ["one"]
