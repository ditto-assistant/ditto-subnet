"""Reveal-anchored planning: payouts and reveal-queue changes, no event reads."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any

import pytest

from ditto.chain.runtime_interface import SOURCE_EMISSION_SURFACE
from ditto.chain.source_emission_verifier import (
    AUDITED_RUNTIME_CODE_HASHES,
    plan_source_emission_range,
    plan_source_emission_range_by_reveals,
)

pytestmark = pytest.mark.asyncio

NETUID = 118
CODE = next(
    code
    for code in sorted(AUDITED_RUNTIME_CODE_HASHES)
    # Any artifact outside the Yuma-gated set: no consensus read needed.
    if not code.startswith("0x5576")
)
KEYS = [(0, "owner"), (1, "validator"), (2, "miner")]
FIRST, LAST = 1001, 1720
# Payouts every 360 blocks; the one before the range anchors the first epoch.
PAYOUTS = (740, 1100, 1460)


@dataclass
class _Key:
    name: str
    value_scale_type: str = "Raw"

    def to_hex(self) -> str:
        return "0x" + self.name.encode().hex()


@dataclass
class RevealChain:
    """A finalized chain where commits and reveals move SN118's queues.

    ``commits[e]`` lists the blocks (inside epoch ``e``) that append to key
    ``e``; ``reveals[e]`` lists the blocks (inside epoch ``e + 1``) that pop one
    entry each. Events mirror the state, so the event planner sees the same
    chain.
    """

    commits: dict[int, list[int]] = field(default_factory=dict)
    reveals: dict[int, list[int]] = field(default_factory=dict)
    v2_commit_at: set[int] = field(default_factory=set)
    reveal_period: dict[int, int] = field(default_factory=dict)
    epoch_shift: tuple[int, int] | None = None
    owner_limit: int = 2
    malformed_hash_at: int | None = None
    # uid -> [(block, new hotkey)]: re-registrations inside the range.
    registrations: dict[int, list[tuple[int, str]]] = field(default_factory=dict)
    # uid -> [(block, new hotkey)]: hotkey swaps, which register nothing.
    swaps: dict[int, list[tuple[int, str]]] = field(default_factory=dict)
    # block -> the coldkey that swapped there; by default each swap's own.
    swapped_by: dict[int, str] = field(default_factory=dict)
    # HotkeySwapOnSubnetInterval; None when the runtime has no such constant.
    swap_interval: int | None = 7200

    def keys_at(self, block: int) -> list[tuple[int, str]]:
        keys = dict(KEYS)
        changes = [
            (at, uid, hotkey)
            for source in (self.registrations, self.swaps)
            for uid, rows in source.items()
            for at, hotkey in rows
        ]
        for at, uid, hotkey in sorted(changes):
            if at <= block:
                keys[uid] = hotkey
        return sorted(keys.items())

    def swap_records_at(self, block: int) -> list[tuple[str, int]]:
        """``LastHotkeySwapOnNetuid``: each coldkey's latest swap by ``block``."""
        records = {"long-ago-coldkey": 500}  # a swap well before the range
        swaps = sorted(at for rows in self.swaps.values() for at, _ in rows)
        for at in swaps:
            if at <= block:
                records[self.swapped_by.get(at, f"coldkey-{at}")] = at
        return sorted(records.items())

    def registered_at(self, block: int) -> list[tuple[int, int]]:
        """``BlockAtRegistration``: the latest registration at or before ``block``."""
        out = {uid: 7 for uid, _ in KEYS}  # all registered long before the range
        for uid, rows in self.registrations.items():
            for at, _hotkey in rows:
                if at <= block:
                    out[uid] = max(out[uid], at)
        return sorted(out.items())

    calls: list[str] = field(default_factory=list)

    @staticmethod
    def hash_of(block: int) -> str:
        return "0x" + f"{block:064x}"

    @staticmethod
    def block_of(block_hash: str) -> int:
        return int(block_hash, 16)

    def epoch(self, block: int) -> int:
        base = 100 + sum(1 for payout in PAYOUTS if payout <= block)
        if self.epoch_shift and self.epoch_shift[0] <= block < self.epoch_shift[1]:
            return base + 1  # the epoch moved without a payout marker
        return base

    def step(self, block: int) -> int:
        return max(payout for payout in PAYOUTS if payout <= block)

    def queue(self, epoch: int, block: int) -> tuple[int, int]:
        """(entries committed so far, entries revealed so far) at ``block``."""
        added = sum(1 for b in self.commits.get(epoch, []) if b <= block)
        taken = sum(1 for b in self.reveals.get(epoch, []) if b <= block)
        return added, taken

    def events(self, block: int) -> list[dict[str, Any]]:
        out = []
        for blocks in self.reveals.values():
            for b in blocks:
                if b == block:
                    out += [
                        _event("WeightsSet", (NETUID, 1)),
                        _event("TimelockedWeightsRevealed", (NETUID, "validator")),
                    ]
        if block in PAYOUTS:
            out.append(_event("IncentiveAlphaEmittedToMiners", (NETUID, [])))
        return out

    async def rpc_request(self, method: str, params: list[Any]) -> Any:
        self.calls.append(method)
        if method == "chain_getBlockHash":
            return {"result": [self.hash_of(n) for n in params[0]]}
        if method == "state_getStorageHash":
            key, at = params
            block = self.block_of(at)
            if key == "0x3a636f6465":
                return {"result": CODE}
            if block == self.malformed_hash_at:
                return {"error": "provider returned no result"}
            name = bytes.fromhex(key[2:]).decode()
            if name.startswith(("Keys:", "BlockAtRegistration:")):
                storage, uid = name.split(":")
                rows = (
                    self.keys_at(block)
                    if storage == "Keys"
                    else self.registered_at(block)
                )
                value = dict(rows).get(int(uid))
                return {
                    "result": "0x" + hashlib.sha256(str(value).encode()).hexdigest()
                }
            assert name.startswith("TimelockedWeightCommits:")
            epoch = int(name.split(":")[1])
            state = self.queue(epoch, block)
            digest = hashlib.sha256(repr(state).encode()).hexdigest()
            return {"result": None if state == (0, 0) else "0x" + digest}
        if method == "state_queryStorageAt":
            (_events_key, _step_key), at = params
            block = self.block_of(at)
            return {
                "result": [
                    {
                        "changes": [
                            [_events_key, "0x" + f"events:{block}".encode().hex()],
                            [_step_key, "0x" + f"step:{block}".encode().hex()],
                        ]
                    }
                ]
            }
        raise AssertionError(method)

    async def decode_scale(self, _type: str, raw: bytes, **_: Any) -> Any:
        kind, block = raw.decode().split(":")
        block_number = int(block)
        if kind == "step":
            return self.step(block_number)
        return self.events(block_number)

    async def create_storage_key(
        self, _pallet: str, name: str, params: list[Any], **_: Any
    ) -> _Key:
        if name == "TimelockedWeightCommits":
            return _Key(f"TimelockedWeightCommits:{params[1]}")
        if name in ("Keys", "BlockAtRegistration"):
            return _Key(f"{name}:{params[1]}")
        return _Key(name)

    async def query(self, *, storage_function: str, block_hash: str, **_: Any):
        self.calls.append(storage_function)
        block = self.block_of(block_hash)
        return {
            "MechanismCountCurrent": 1,
            "CommitRevealWeightsEnabled": True,
            "RevealPeriodEpochs": self.reveal_period.get(block, 1),
            "LastMechansimStepBlock": self.step(block),
            "SubnetEpochIndex": self.epoch(block),
            "Tempo": 360,
            "OwnerHyperparamRateLimit": self.owner_limit,
        }[storage_function]

    async def query_map(self, *, storage_function: str, block_hash: str, **_: Any):
        self.calls.append(storage_function)
        block = self.block_of(block_hash)
        rows: list[tuple[Any, Any]] = []
        if storage_function == "Keys":
            rows = self.keys_at(block)
        elif storage_function == "BlockAtRegistration":
            rows = self.registered_at(block)
        elif storage_function == "LastHotkeySwapOnNetuid":
            rows = self.swap_records_at(block)
        elif block in self.v2_commit_at:
            assert storage_function == "WeightCommits"
            rows = [("validator", ["hash"])]

        async def iterate():
            for row in rows:
                yield row

        return iterate()

    async def get_block_metadata(self, **_):
        raise AssertionError("audited artifacts need no metadata")

    async def get_constant(self, module_name: str, constant_name: str, **_: Any):
        assert (module_name, constant_name) == (
            "SubtensorModule",
            "HotkeySwapOnSubnetInterval",
        )
        return self.swap_interval


def _event(name: str, attributes: Any) -> dict[str, Any]:
    return {
        "phase": "Initialization",
        "module_id": "SubtensorModule",
        "event_id": name,
        "event": {"attributes": attributes},
    }


def _chain() -> RevealChain:
    """Epoch 101 spans 740..1099, 102 spans 1100..1459, 103 from 1460."""
    return RevealChain(
        commits={
            101: [800, 950, 1050],  # revealed during epoch 102
            102: [1150, 1300],  # revealed during epoch 103
            103: [1500, 1600],  # still pending at the range end
        },
        reveals={
            100: [1010],  # a straggler revealed in the first (partial) epoch
            101: [1100, 1101, 1233],  # two at the payout block's turn, one later
            102: [1460, 1702],
        },
    )


async def _plan(chain: RevealChain, first: int = FIRST, last: int = LAST):
    return await plan_source_emission_range_by_reveals(
        chain, netuid=NETUID, first_block=first, last_block=last
    )


async def test_finds_every_payout_and_reveal_block_and_no_commit() -> None:
    plan = await _plan(_chain())
    assert not plan.full_scan
    assert plan.interesting == (1010, 1100, 1101, 1233, 1460, 1702)


async def test_matches_the_event_planner_on_the_same_chain() -> None:
    chain = _chain()
    by_events = await plan_source_emission_range(
        chain, netuid=NETUID, first_block=FIRST, last_block=LAST
    )
    assert (await _plan(chain)).interesting == by_events.interesting


async def test_reads_a_small_fraction_of_the_range() -> None:
    chain = _chain()
    await _plan(chain)
    storage_reads = [c for c in chain.calls if c != "chain_getBlockHash"]
    # 720 blocks: the event planner makes one read per block.
    assert len(storage_reads) < 160


async def test_payouts_only_when_nothing_was_revealed() -> None:
    chain = RevealChain(commits={103: [1500]})
    assert (await _plan(chain)).interesting == (1100, 1460)


async def test_range_starting_on_a_payout_includes_its_own_reveals() -> None:
    chain = _chain()
    plan = await _plan(chain, first=1100, last=1300)
    assert plan.interesting == (1100, 1101, 1233)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda c: c.v2_commit_at.add(1460), "v2_commits"),
        (lambda c: c.reveal_period.update({LAST: 2}), "reveal_period"),
        (lambda c: setattr(c, "epoch_shift", (1300, 1460)), "epoch_without_payout"),
        # One owner change per 360 blocks would allow off-then-on inside 720.
        (lambda c: setattr(c, "owner_limit", 1), "owner_rate_limit"),
        # A coldkey could swap twice inside the range, and its swap record
        # would keep only the second.
        (lambda c: setattr(c, "swap_interval", LAST - FIRST), "hotkey_swap_interval"),
        (lambda c: setattr(c, "swap_interval", None), "hotkey_swap_interval"),
    ],
    ids=[
        "v2-commit-store-in-use",
        "reveal-period-changed",
        "epoch-without-payout",
        "owner-could-toggle-controls-inside-the-range",
        "swap-cooldown-shorter-than-the-range",
        "swap-cooldown-unknown",
    ],
)
async def test_unsupported_shapes_fall_back_to_the_full_scan(mutate, reason) -> None:
    chain = _chain()
    mutate(chain)
    plan = await _plan(chain)
    assert (plan.full_scan, plan.full_scan_reason) == (True, reason)


async def test_malformed_storage_hash_is_not_read_as_unchanged() -> None:
    chain = _chain()
    # The reveal key is absent at the range end, so a malformed answer at the
    # start would otherwise compare equal to it and hide the reveals.
    chain.malformed_hash_at = FIRST - 1
    with pytest.raises(ValueError, match="missing storage hash"):
        await _plan(chain)


@pytest.mark.parametrize(
    "planner", [plan_source_emission_range, plan_source_emission_range_by_reveals]
)
async def test_a_reregistered_uid_is_located_and_read_instead_of_a_full_scan(
    planner,
) -> None:
    chain = _chain()
    chain.registrations = {2: [(1333, "newminer")]}
    plan = await planner(chain, netuid=NETUID, first_block=FIRST, last_block=LAST)
    assert not plan.full_scan
    assert 1333 in plan.interesting
    # Everything else is planned exactly as without the registration.
    assert set(plan.interesting) - {1333} == {1010, 1100, 1101, 1233, 1460, 1702}


async def test_two_registrations_of_one_uid_are_both_found() -> None:
    chain = _chain()
    chain.registrations = {2: [(1200, "second"), (1600, "third")]}
    plan = await _plan(chain)
    assert {1200, 1600} <= set(plan.interesting)


async def test_too_many_changed_uids_plan_the_range_in_full(monkeypatch) -> None:
    from ditto.chain import source_emission_verifier

    monkeypatch.setattr(source_emission_verifier, "_MAX_IDENTITY_CHANGES", 0)
    chain = _chain()
    chain.registrations = {2: [(1333, "newminer")]}
    plan = await _plan(chain)
    assert (plan.full_scan, plan.full_scan_reason) == (True, "identity_changes")


async def test_a_uid_that_returns_to_its_old_hotkey_is_still_found() -> None:
    # A -> B -> A: Keys match at both ends; BlockAtRegistration does not.
    chain = _chain()
    chain.registrations = {2: [(1200, "intruder"), (1550, "miner")]}
    assert chain.keys_at(FIRST - 1) == chain.keys_at(LAST)
    plan = await _plan(chain)
    assert not plan.full_scan
    assert {1200, 1550} <= set(plan.interesting)


@pytest.mark.parametrize(
    "planner", [plan_source_emission_range, plan_source_emission_range_by_reveals]
)
@pytest.mark.parametrize(
    ("registrations", "swaps"),
    [
        # A registration replaces the miner; a swap then restores its hotkey.
        ({2: [(1200, "intruder")]}, {2: [(1550, "miner")]}),
        # A swap moves the miner away; a registration then restores its hotkey.
        ({2: [(1550, "miner")]}, {2: [(1200, "swapped")]}),
    ],
)
async def test_a_registration_and_a_swap_that_restore_a_hotkey_are_both_found(
    planner, registrations, swaps
) -> None:
    chain = _chain()
    chain.registrations, chain.swaps = registrations, swaps
    assert chain.keys_at(FIRST - 1) == chain.keys_at(LAST)
    plan = await planner(chain, netuid=NETUID, first_block=FIRST, last_block=LAST)
    assert not plan.full_scan
    assert {1200, 1550} <= set(plan.interesting)


@pytest.mark.parametrize(
    "planner", [plan_source_emission_range, plan_source_emission_range_by_reveals]
)
async def test_a_hotkey_swapped_away_and_back_is_found(planner) -> None:
    # Two coldkeys (the first swapped its coldkey in between) each swap once,
    # so the per-coldkey cooldown allows both inside one range. Keys and
    # BlockAtRegistration match at both ends; the swap records do not.
    chain = _chain()
    chain.swaps = {2: [(1200, "swapped"), (1550, "miner")]}
    assert chain.keys_at(FIRST - 1) == chain.keys_at(LAST)
    assert chain.registered_at(FIRST - 1) == chain.registered_at(LAST)
    plan = await planner(chain, netuid=NETUID, first_block=FIRST, last_block=LAST)
    assert not plan.full_scan
    assert {1200, 1550} <= set(plan.interesting)
    # The record from before the range is not a block of it.
    assert all(FIRST <= block <= LAST for block in plan.interesting)


async def test_a_hotkey_swap_is_found_without_a_registration() -> None:
    chain = _chain()
    chain.swaps = {1: [(1400, "validator2")]}
    plan = await _plan(chain)
    assert not plan.full_scan
    assert 1400 in plan.interesting


async def test_a_failed_bisection_leaves_no_reads_running() -> None:
    chain = _chain()
    chain.registrations = {1: [(1200, "a")], 2: [(1300, "b")]}
    chain.malformed_hash_at = (FIRST - 1 + LAST) // 2  # every bisection's first probe
    with pytest.raises(ValueError, match="missing storage hash"):
        await _plan(chain)
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_every_storage_item_a_planner_reads_is_in_the_pinned_surface(
    monkeypatch,
) -> None:
    # A runtime admitted on its interface alone must not change, unseen, the
    # shape of anything a planner decodes.
    chain = _chain()
    chain.registrations = {2: [(1333, "newminer")]}
    chain.swaps = {1: [(1400, "validator2")]}
    create = chain.create_storage_key
    keyed: list[str] = []

    async def recording(pallet: str, name: str, params: list[Any], **kw: Any) -> _Key:
        keyed.append(name)
        return await create(pallet, name, params, **kw)

    monkeypatch.setattr(chain, "create_storage_key", recording)
    for planner in (plan_source_emission_range, plan_source_emission_range_by_reveals):
        await planner(chain, netuid=NETUID, first_block=FIRST, last_block=LAST)
    # System.Events is pinned by its consumed variants, never whole.
    read = {
        name
        for name in (*chain.calls, *keyed)
        if not name.startswith(("chain_", "state_")) and name != "Events"
    }
    assert read - {name for _, name in SOURCE_EMISSION_SURFACE.storage} == set()


async def test_a_failed_storage_key_leaves_no_reads_running(monkeypatch) -> None:
    chain = _chain()
    chain.registrations = {1: [(1200, "a")], 2: [(1300, "b")]}
    create = chain.create_storage_key

    async def failing(pallet: str, name: str, params: list[Any], **kw: Any) -> _Key:
        if name == "Keys" and params[1] == 2:
            raise ValueError("no such storage item")
        return await create(pallet, name, params, **kw)

    monkeypatch.setattr(chain, "create_storage_key", failing)
    with pytest.raises(ValueError, match="no such storage item"):
        await _plan(chain)
    assert asyncio.all_tasks() == {asyncio.current_task()}
