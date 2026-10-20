"""Sparse replay planning: skip only blocks the full reader would ignore."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from ditto.chain.source_emission_verifier import (
    AUDITED_RUNTIME_CODE_HASHES,
    plan_source_emission_range,
)

pytestmark = pytest.mark.asyncio

NETUID = 118
CODE = next(iter(sorted(AUDITED_RUNTIME_CODE_HASHES)))
KEYS = [(0, "owner"), (1, "validator"), (2, "miner")]


def _event(module: str, name: str, attributes: Any = None) -> dict[str, Any]:
    return {
        "phase": "Initialization",
        "module_id": module,
        "event_id": name,
        "event": {"attributes": attributes},
    }


def _weights_set(netuid: int) -> dict[str, Any]:
    return _event("SubtensorModule", "WeightsSet", (netuid, 1))


@dataclass
class _Key:
    name: str
    value_scale_type: str

    def to_hex(self) -> str:
        return "0x" + self.name.encode().hex()


@dataclass
class FakeChain:
    """Answers exactly the calls ``plan_source_emission_range`` makes."""

    first: int
    last: int
    events: dict[int, Any] = field(default_factory=dict)
    steps: dict[int, int] = field(default_factory=dict)
    code: dict[int, str] = field(default_factory=dict)
    keys: dict[int, list[tuple[int, str]]] = field(default_factory=dict)
    controls: dict[int, tuple[Any, Any]] = field(default_factory=dict)
    missing_step: set[int] = field(default_factory=set)
    consensus: dict[int, str] = field(default_factory=dict)
    storage_reads: list[int] = field(default_factory=list)

    @staticmethod
    def hash_of(block: int) -> str:
        return "0x" + f"{block:064x}"

    @staticmethod
    def block_of(block_hash: str) -> int:
        return int(block_hash, 16)

    async def rpc_request(self, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "chain_getBlockHash":
            return {"result": [self.hash_of(b) for b in params[0]]}
        if method == "state_getStorageHash":
            block = self.block_of(params[1])
            return {"result": self.code.get(block, CODE)}
        if method == "state_queryStorageAt":
            (events_key, step_key), at = params
            block = self.block_of(at)
            self.storage_reads.append(block)
            changes = [[events_key, "0x00"]]
            if block not in self.missing_step:
                changes.append([step_key, "0x00"])
            return {"result": [{"block": at, "changes": changes}]}
        raise AssertionError(f"unexpected RPC {method}")

    async def create_storage_key(
        self, pallet: str, name: str, params: list[Any], block_hash: str
    ) -> _Key:
        del pallet, params, block_hash
        return _Key(name, name)

    async def decode_scale(
        self, type_string: str, scale_bytes: bytes, block_hash: str
    ) -> Any:
        del scale_bytes
        block = self.block_of(block_hash)
        if type_string == "Events":
            return self.events.get(block, [_event("System", "ExtrinsicSuccess")])
        return self.steps.get(block, self.first - 7)

    async def query(
        self, module: str, storage_function: str, params: list[Any], block_hash: str
    ) -> Any:
        del module, params
        if storage_function == "SubnetEpochConsensus":
            block = self.block_of(block_hash)
            # Like the library: a state is decoded with the metadata of the
            # runtime stored at its parent, which before v475 has no item.
            if self.code.get(block - 1, CODE) != _V475:
                raise ValueError('Storage function "SubnetEpochConsensus" not found')
            return self.consensus.get(block, "Yuma")
        mechanisms, commit_reveal = self.controls.get(
            self.block_of(block_hash), (1, True)
        )
        return (
            mechanisms
            if storage_function == "MechanismCountCurrent"
            else (commit_reveal)
        )

    async def query_map(self, **kwargs: Any) -> Any:
        rows = self.keys.get(self.block_of(kwargs["block_hash"]), KEYS)

        async def iterate():
            for row in rows:
                yield row

        return iterate()


async def _plan(chain: FakeChain):
    return await plan_source_emission_range(
        chain, netuid=NETUID, first_block=chain.first, last_block=chain.last
    )


async def test_only_blocks_the_reader_acts_on_are_interesting() -> None:
    chain = FakeChain(first=101, last=120)
    chain.events = {
        103: [_weights_set(NETUID)],
        104: [_weights_set(7)],  # another subnet's write is quiet
        105: [_event("SubtensorModule", "TimelockedWeightsRevealed", (NETUID, "v"))],
        106: [
            _event(
                "SubtensorModule",
                "IncentiveAlphaEmittedToMiners",
                {"netuid": NETUID, "emissions": []},
            )
        ],
        107: [_event("System", "CodeUpdated")],
        108: [_event("SubtensorModule", "HotkeySwapped", ("c", "a", "b"))],
        109: [_event("SubtensorModule", "NeuronRegistered", (7, 3, "x"))],
        110: [_event("SubtensorModule", "StakeAdded", ("c", "h", 1, 1, NETUID, 0))],
        114: [_event("SubtensorModule", "NeuronDeregistered", (NETUID, 2))],
        117: [_event("SubtensorModule", "NullUidsPruningProgress", {"netuid": 7})],
        111: [_event("SubtensorModule", "WeightsSet", ("malformed",))],
        112: ["not an event"],
        113: None,
    }
    chain.steps = {115: 115, 116: 115}
    chain.missing_step = {118}

    plan = await _plan(chain)

    assert plan.full_scan is False
    assert plan.runtime_code_hash == CODE
    assert plan.interesting == (
        103,
        105,
        106,
        107,
        108,
        109,
        111,
        112,
        113,
        114,
        115,
        117,
        118,
    )
    assert set(plan.hashes) == set(range(100, 121))
    assert sorted(chain.storage_reads) == list(range(101, 121))


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda c: c.code.__setitem__(120, "0x" + "ab" * 32), "runtime_changed"),
        (
            lambda c: [c.code.__setitem__(b, "0x" + "cd" * 32) for b in (100, 120)],
            "runtime_not_admitted",
        ),
        (lambda c: c.keys.__setitem__(120, [*KEYS, (3, "newcomer")]), "uid_mapping"),
        (
            lambda c: c.keys.__setitem__(100, [(0, "owner"), (2, "miner")]),
            "uid_mapping",
        ),
        (lambda c: c.controls.__setitem__(100, (2, True)), "mechanism_count"),
        (lambda c: c.controls.__setitem__(120, (1, False)), "commit_reveal_off"),
    ],
    ids=[
        "runtime-changed",
        "unaudited-runtime",
        "uid-registered",
        "incomplete-mapping",
        "multi-mechanism",
        "commit-reveal-off",
    ],
)
async def test_structural_change_or_invalid_endpoint_requests_full_scan(
    mutate, reason
) -> None:
    chain = FakeChain(first=101, last=120)
    mutate(chain)

    plan = await _plan(chain)

    assert plan.full_scan is True
    assert plan.full_scan_reason == reason
    assert plan.interesting == ()
    assert chain.storage_reads == []


async def test_missing_block_hash_fails_closed() -> None:
    chain = FakeChain(first=101, last=103)

    async def rpc_request(_method, params):
        return {"result": [chain.hash_of(params[0][0]), None, None, None]}

    chain.rpc_request = rpc_request  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="missing historical block hashes"):
        await _plan(chain)


async def test_invalid_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid source-emission range"):
        await plan_source_emission_range(
            FakeChain(first=5, last=4), netuid=NETUID, first_block=5, last_block=4
        )


async def test_a_failed_block_read_cancels_its_siblings() -> None:
    import asyncio

    chain = FakeChain(first=101, last=140)
    original = chain.rpc_request
    started: list[int] = []
    finished: list[int] = []

    async def rpc_request(method, params):
        if method != "state_queryStorageAt":
            return await original(method, params)
        block = chain.block_of(params[1])
        started.append(block)
        if block == 101:
            raise RuntimeError("Historical work rate limit exceeded")
        await asyncio.sleep(0.05)
        finished.append(block)
        return await original(method, params)

    chain.rpc_request = rpc_request  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="rate limit"):
        await _plan(chain)
    await asyncio.sleep(0.2)
    # The failure stops queued reads instead of letting all 40 run.
    assert len(started) < 40
    assert finished == []


_V475 = "0x557634c8c31bc639ea6552e297dcfb781cd7d9bdd491a5352c151305db33d3a0"


@pytest.mark.parametrize(
    ("consensus", "full_scan"),
    [({}, False), ({100: "Null"}, True), ({120: "Null"}, True)],
    ids=["yuma", "null-at-start", "null-at-end"],
)
async def test_v475_endpoints_require_yuma(consensus, full_scan) -> None:
    chain = FakeChain(first=101, last=120)
    chain.code = {99: _V475, 100: _V475, 119: _V475, 120: _V475}
    chain.consensus = consensus
    chain.events = {
        110: [_event("SubtensorModule", "EpochConsensusSet", (NETUID, "Null"))]
    }

    plan = await _plan(chain)

    assert plan.full_scan is full_scan
    if not full_scan:
        # A consensus switch inside the range gets the full (fail-closed) read.
        assert plan.interesting == (110,)


async def test_v475_range_starting_on_its_upgrade_block_is_planned() -> None:
    # Block 100 stores v475's code but was written by the previous runtime,
    # whose metadata has no SubnetEpochConsensus (Finney block 9233781).
    chain = FakeChain(first=101, last=120)
    chain.code = dict.fromkeys(range(100, 121), _V475)

    plan = await _plan(chain)

    assert not plan.full_scan
