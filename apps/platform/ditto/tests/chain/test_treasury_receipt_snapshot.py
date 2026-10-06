"""Per-proof hash reuse does not cache effects, failures or other proofs."""

from collections import Counter

import pytest

from ditto.chain.treasury_receipts import _FinalizedReceiptSnapshot, finalized_block
from ditto_screening_protocol.collector_receipts import (
    AUDITED_COLLECTOR_CODE_HASH,
    FINNEY_GENESIS,
)


class Reads:
    def __init__(self):
        self.calls = Counter()
        self.head = 200

    async def get_chain_finalised_head(self):
        self.calls["head"] += 1
        return f"block{self.head}"

    async def get_block_number(self, at):
        self.calls["number", at] += 1
        return int(at.removeprefix("block"))

    async def get_block_hash(self, number):
        self.calls["hash", number] += 1
        return FINNEY_GENESIS if number == 0 else f"block{number}"

    async def rpc_request(self, method, params):
        self.calls[method, tuple(params)] += 1
        return {
            "result": AUDITED_COLLECTOR_CODE_HASH
            if method == "state_getStorageHash"
            else self.calls[method, tuple(params)]
        }

    async def query(self, *_args, **_kwargs):
        self.calls["query"] += 1
        return self.calls["query"]


@pytest.mark.asyncio
async def test_finality_and_exact_runtime_hash_reads_are_shared_in_one_proof():
    raw = Reads()
    snapshot = _FinalizedReceiptSnapshot(raw)
    assert await finalized_block(
        snapshot, 100, FINNEY_GENESIS
    ) == await finalized_block(snapshot, 100, FINNEY_GENESIS)
    await finalized_block(snapshot, 101, FINNEY_GENESIS)
    assert raw.calls["head"] == raw.calls["number", "block200"] == 1
    assert raw.calls["hash", 0] == raw.calls["hash", 200] == 1
    assert raw.calls["state_getStorageHash", ("0x3a636f6465", "block100")] == 1
    assert raw.calls["state_getStorageHash", ("0x3a636f6465", "block101")] == 1
    assert await snapshot.query() == 1
    assert await snapshot.query() == 2  # no identity/storage/effect memoization
    assert await snapshot.rpc_request("chain_getBlock", ["block100"]) == {"result": 1}
    assert await snapshot.rpc_request("chain_getBlock", ["block100"]) == {"result": 2}


@pytest.mark.asyncio
async def test_new_proof_gets_new_finality_and_hashes():
    raw = Reads()
    await finalized_block(_FinalizedReceiptSnapshot(raw), 100, FINNEY_GENESIS)
    raw.head = 201
    await finalized_block(_FinalizedReceiptSnapshot(raw), 100, FINNEY_GENESIS)
    assert raw.calls["head"] == 2
    assert raw.calls["state_getStorageHash", ("0x3a636f6465", "block100")] == 2


@pytest.mark.asyncio
async def test_failed_rpc_is_not_cached_and_cached_dict_is_not_mutable_by_caller():
    raw = Reads()
    original = raw.rpc_request
    attempts = 0

    async def fail_once(method, params):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("unavailable")
        return await original(method, params)

    raw.rpc_request = fail_once
    snapshot = _FinalizedReceiptSnapshot(raw)
    args = ("state_getStorageHash", ["0x3a636f6465", "block100"])
    with pytest.raises(ConnectionError):
        await snapshot.rpc_request(*args)
    result = await snapshot.rpc_request(*args)
    result["result"] = "tampered"
    assert await snapshot.rpc_request(*args) == {"result": AUDITED_COLLECTOR_CODE_HASH}
    assert attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "unknown", "0x" + "0" * 64])
async def test_unknown_runtime_hash_still_refuses(bad):
    raw = Reads()

    async def unsupported(*_args, **_kwargs):
        return {"result": bad}

    raw.rpc_request = unsupported
    with pytest.raises(ValueError, match="not audited"):
        await finalized_block(_FinalizedReceiptSnapshot(raw), 100, FINNEY_GENESIS)
