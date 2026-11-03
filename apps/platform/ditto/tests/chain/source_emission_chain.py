"""One finalized Subtensor state model for every source-emission read path.

``SubtensorTimeline`` applies SN118 chain actions block by block, the way the
audited runtime does (and the way the range planners assume it does):

* a payout sets ``LastMechansimStepBlock`` to its block and moves
  ``SubnetEpochIndex`` on by one;
* a commit-reveal v3 commit appends ``(who, block, ciphertext, round)`` to
  ``TimelockedWeightCommits(netuid, epoch)`` in an extrinsic;
* a matured reveal pops the committer's entry from the queue of the epoch
  ``RevealPeriodEpochs`` before the current one (on a payout block, the epoch
  the payout starts), writes ``Weights`` and emits ``WeightsSet`` and
  ``TimelockedWeightsRevealed`` during initialization, ahead of the payout's
  ``IncentiveAlphaEmittedToMiners``;
* a registration replaces a UID's hotkey and ``BlockAtRegistration``, drops
  its own weight row and zeroes every weight on it;
* a hotkey swap replaces the hotkey and records the block under its owner's
  coldkey in ``LastHotkeySwapOnNetuid``; that coldkey cannot swap again on
  SN118 until ``HotkeySwapOnSubnetInterval`` has passed, and records older
  than the interval are pruned on SN118's slot of it;
* a coldkey swap hands every hotkey to the new coldkey and carries no swap
  record over, so the new coldkey can swap at once.

Each block keeps the events that produced its state. ``SourceEmissionChain``
serves that one model to the full per-block reader
(``read_source_emission_block``), to both range planners (batched
``chain_getBlockHash``, ``state_getStorageHash`` for every key they bisect,
``state_queryStorageAt`` and ``decode_scale``) and to the payout receipt the
collector fetches. Nothing is scripted per call site, so two read paths can
disagree only because of what they read, never because the fake answered
them differently.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ditto.chain import source_emission_verifier as verifier
from ditto.chain.errors import ChainEmissionReceiptUnavailable
from ditto.chain.models import (
    ChainMinerEarning,
    ChainMinerEmissionReceipt,
    ChainWeight,
    ChainWeightVector,
)

NETUID = 118
FOREIGN_NETUID = 7
# Audited artifacts, so admission needs no metadata. CODE is outside the
# Yuma-gated set (no SubnetEpochConsensus read); GATED_CODE is a v475+ artifact,
# read only while SubnetEpochConsensus is Yuma.
CODE = next(
    code
    for code in sorted(verifier.AUDITED_RUNTIME_CODE_HASHES)
    if code not in verifier._YUMA_GATED_RUNTIME_CODE_HASHES
)
GATED_CODE = sorted(verifier._YUMA_GATED_RUNTIME_CODE_HASHES)[0]
CODE_KEY = "0x" + b":code".hex()
OWNER = "owner"
TEMPO = 360
OWNER_HYPERPARAM_RATE_LIMIT = 2
REVEAL_PERIOD_EPOCHS = 1
# Finney's HotkeySwapOnSubnetInterval: one day of 12-second blocks.
HOTKEY_SWAP_INTERVAL = 7200

# The model starts here; everything before it is settled history.
GENESIS_BLOCK = 600
GENESIS_STEP = 380
GENESIS_EPOCH = 101
GENESIS_KEYS = {
    0: OWNER,
    1: "validator-a",
    2: "validator-b",
    3: "miner-a",
    4: "miner-b",
}
GENESIS_REGISTERED = {0: 7, 1: 11, 2: 13, 3: 17, 4: 19}
# Validators weight the owner (burn) and both miners.
GENESIS_WEIGHTS = {
    1: ((0, 6553), (3, 39321), (4, 19661)),
    2: ((0, 6553), (3, 29490), (4, 29492)),
}
# Per UID, not per hotkey: StakeWeight, Active and ValidatorPermit are vectors.
STAKE_WEIGHT = (0, 40000, 25535, 0, 0)
VALIDATOR_PERMIT = (False, True, True, False, False)
TIMESTAMP_ZERO = 1_789_680_000

Vector = tuple[tuple[int, int], ...]
Commit = tuple[str, int, str, int]  # (who, commit block, ciphertext, round)

_KNOWN_STORAGE = {
    ("System", "Events"),
    *(
        ("SubtensorModule", name)
        for name in (
            "Active",
            "BlockAtRegistration",
            "CommitRevealWeightsEnabled",
            "Keys",
            "LastHotkeySwapOnNetuid",
            "LastMechansimStepBlock",
            "MechanismCountCurrent",
            "OwnerHyperparamRateLimit",
            "RevealPeriodEpochs",
            "StakeWeight",
            "SubnetEpochConsensus",
            "SubnetEpochIndex",
            "SubnetOwnerHotkey",
            "Tempo",
            "TimelockedWeightCommits",
            "ValidatorPermit",
            "WeightCommits",
            "Weights",
        )
    ),
}


def hash_of(block: int) -> str:
    return "0x" + f"{block:064x}"


def _event(
    name: str,
    attributes: Any,
    *,
    phase: str = "Initialization",
    module: str = "SubtensorModule",
) -> dict[str, Any]:
    return {
        "phase": phase,
        "module_id": module,
        "event_id": name,
        "event": {"attributes": attributes},
    }


def foreign_reveal() -> dict[str, Any]:
    """Another subnet's weight write: quiet for SN118."""
    return _event("WeightsSet", (FOREIGN_NETUID, 3))


def foreign_payout() -> dict[str, Any]:
    return _event(
        "IncentiveAlphaEmittedToMiners",
        {"netuid": FOREIGN_NETUID, "emissions": [0, 5, 7]},
    )


def foreign_registration() -> dict[str, Any]:
    """Another subnet's registration: the event planner reads it in full."""
    return _event(
        "NeuronRegistered", (FOREIGN_NETUID, 12, "elsewhere"), phase="ApplyExtrinsic"
    )


def stake_added() -> dict[str, Any]:
    return _event(
        "StakeAdded",
        ("coldkey", "validator-a", 1_000_000, 990_000, NETUID, 0),
        phase="ApplyExtrinsic",
    )


def _commit_hash(ciphertext: str) -> str:
    return (
        "0x"
        + hashlib.blake2b(bytes.fromhex(ciphertext[2:]), digest_size=32).hexdigest()
    )


@dataclass(frozen=True)
class BlockState:
    """SN118 storage after ``block`` was executed, and the block's events."""

    keys: dict[int, str]
    registered: dict[int, int]
    weights: dict[int, Vector]
    commits: dict[int, tuple[Commit, ...]]
    step: int
    epoch: int
    events: tuple[dict[str, Any], ...]
    # Coldkey -> block of its last hotkey swap on SN118.
    swap_records: dict[str, int]
    # The runtime's ``:code`` hash stored after this block.
    code: str

    def map_storage(self) -> dict[str, dict[Any, Any]]:
        """``netuid``-prefixed maps, as ``query_map`` iterates them."""
        return {
            "Keys": self.keys,
            "BlockAtRegistration": self.registered,
            "Weights": {uid: list(vector) for uid, vector in self.weights.items()},
            # An emptied queue is removed, so its storage hash reads as absent.
            "TimelockedWeightCommits": {
                epoch: list(queue) for epoch, queue in self.commits.items() if queue
            },
            # The v2 commit store is never used on this chain.
            "WeightCommits": {},
            "LastHotkeySwapOnNetuid": self.swap_records,
        }

    def value_storage(self) -> dict[str, Any]:
        """``netuid``-keyed plain values."""
        uids = range(len(self.keys))
        return {
            "MechanismCountCurrent": 1,
            "CommitRevealWeightsEnabled": True,
            "RevealPeriodEpochs": REVEAL_PERIOD_EPOCHS,
            "Tempo": TEMPO,
            "LastMechansimStepBlock": self.step,
            "SubnetEpochIndex": self.epoch,
            "SubnetEpochConsensus": "Yuma",
            "SubnetOwnerHotkey": OWNER,
            "StakeWeight": [STAKE_WEIGHT[uid] for uid in uids],
            "Active": [True for _ in uids],
            "ValidatorPermit": [VALIDATOR_PERMIT[uid] for uid in uids],
        }


class SubtensorTimeline:
    """SN118 chain actions by block; ``build`` replays them into states."""

    def __init__(
        self,
        *,
        payouts: Iterable[int],
        head: int,
        swap_interval: int = HOTKEY_SWAP_INTERVAL,
    ) -> None:
        self.payouts = frozenset(payouts)
        self.head = head
        self.swap_interval = swap_interval
        self._actions: dict[int, list[tuple[str, tuple[Any, ...]]]] = {}

    def _add(self, block: int, kind: str, *args: Any) -> SubtensorTimeline:
        if not GENESIS_BLOCK < block <= self.head:
            raise ValueError(f"block {block} is outside the modelled chain")
        self._actions.setdefault(block, []).append((kind, args))
        return self

    def commit(self, block: int, who: str) -> SubtensorTimeline:
        return self._add(block, "commit", who)

    def reveal(
        self, block: int, who: str, weights: Iterable[tuple[int, int]]
    ) -> SubtensorTimeline:
        return self._add(block, "reveal", who, tuple(weights))

    def register(self, block: int, uid: int, hotkey: str) -> SubtensorTimeline:
        return self._add(block, "register", uid, hotkey)

    def swap(self, block: int, uid: int, hotkey: str) -> SubtensorTimeline:
        return self._add(block, "swap", uid, hotkey)

    def coldkey_swap(self, block: int, old: str, new: str) -> SubtensorTimeline:
        return self._add(block, "coldkey_swap", old, new)

    def upgrade(self, block: int, code: str) -> SubtensorTimeline:
        """``set_code`` in ``block``: the state after it stores ``code``."""
        return self._add(block, "upgrade", code)

    def noise(self, block: int, *events: dict[str, Any]) -> SubtensorTimeline:
        for event in events:
            self._add(block, "noise", event)
        return self

    def build(self, code: str = CODE) -> SourceEmissionChain:
        keys = dict(GENESIS_KEYS)
        registered = dict(GENESIS_REGISTERED)
        weights: dict[int, Vector] = dict(GENESIS_WEIGHTS)
        commits: dict[int, tuple[Commit, ...]] = {}
        step, epoch = GENESIS_STEP, GENESIS_EPOCH
        owners = {hotkey: f"{hotkey}-coldkey" for hotkey in keys.values()}
        swap_records: dict[str, int] = {}
        interval = self.swap_interval
        states = {
            GENESIS_BLOCK: BlockState(
                dict(keys),
                dict(registered),
                dict(weights),
                {},
                step,
                epoch,
                (),
                {},
                code,
            )
        }
        for block in range(GENESIS_BLOCK + 1, self.head + 1):
            actions = self._actions.get(block, [])
            # clean_up_hotkey_swap_records: SN118's turn comes once per interval.
            if block % interval == NETUID % interval:
                swap_records = {
                    coldkey: at
                    for coldkey, at in swap_records.items()
                    if at + interval >= block
                }
            initialization: list[dict[str, Any]] = []
            extrinsics: list[dict[str, Any]] = []
            payout = block in self.payouts
            if payout:
                step, epoch = block, epoch + 1
            # block_step reveals matured commits before the epoch pays out.
            for kind, args in actions:
                if kind != "reveal":
                    continue
                who, vector = args
                uid = next((u for u, h in keys.items() if h == who), None)
                if uid is None:
                    raise ValueError(f"{who} reveals at {block} but is not registered")
                if any(
                    target not in keys or not 0 <= w <= 65535 for target, w in vector
                ):
                    raise ValueError(f"invalid weights revealed at {block}")
                reveal_epoch = epoch - REVEAL_PERIOD_EPOCHS
                queue = list(commits.get(reveal_epoch, ()))
                entry = next((c for c in queue if c[0] == who), None)
                if entry is None:
                    raise ValueError(f"{who} has no commit for epoch {reveal_epoch}")
                queue.remove(entry)
                commits[reveal_epoch] = tuple(queue)
                weights[uid] = vector
                initialization += [
                    _event("WeightsSet", (NETUID, uid)),
                    _event("TimelockedWeightsRevealed", (NETUID, who)),
                ]
            if payout:
                emissions = [0] * len(keys)
                for vector in weights.values():
                    for target, w in vector:
                        emissions[target] += w * 1000
                initialization.append(
                    _event(
                        "IncentiveAlphaEmittedToMiners",
                        {"netuid": NETUID, "emissions": emissions},
                    )
                )
            for kind, args in actions:
                if kind == "commit":
                    (who,) = args
                    ciphertext = (
                        "0x" + hashlib.sha256(f"{who}@{block}".encode()).hexdigest()
                    )
                    reveal_round = 20_000_000 + block
                    commits[epoch] = (
                        *commits.get(epoch, ()),
                        (who, block, ciphertext, reveal_round),
                    )
                    extrinsics.append(
                        _event(
                            "TimelockedWeightsCommitted",
                            (who, NETUID, _commit_hash(ciphertext), reveal_round),
                            phase="ApplyExtrinsic",
                        )
                    )
                elif kind == "register":
                    uid, hotkey = args
                    if hotkey in keys.values():
                        raise ValueError(f"{hotkey} is already registered")
                    keys[uid] = hotkey
                    registered[uid] = block
                    owners.setdefault(hotkey, f"{hotkey}-coldkey")
                    weights.pop(uid, None)
                    weights = {
                        row: tuple((t, 0 if t == uid else w) for t, w in vector)
                        for row, vector in weights.items()
                    }
                    extrinsics.append(
                        _event(
                            "NeuronRegistered",
                            (NETUID, uid, hotkey),
                            phase="ApplyExtrinsic",
                        )
                    )
                elif kind == "swap":
                    uid, hotkey = args
                    if hotkey in keys.values():
                        raise ValueError(f"{hotkey} is already registered")
                    coldkey = owners[keys[uid]]
                    last = swap_records.get(coldkey)
                    if last is not None and last + interval >= block:
                        raise ValueError(f"{coldkey} swapped at {last}, too soon")
                    old, keys[uid] = keys[uid], hotkey
                    owners[hotkey] = coldkey
                    swap_records[coldkey] = block
                    extrinsics.append(
                        _event(
                            "HotkeySwapped",
                            {
                                "coldkey": coldkey,
                                "old_hotkey": old,
                                "new_hotkey": hotkey,
                            },
                            phase="ApplyExtrinsic",
                        )
                    )
                elif kind == "upgrade":
                    (code,) = args
                    extrinsics.append(
                        _event(
                            "CodeUpdated", {}, phase="ApplyExtrinsic", module="System"
                        )
                    )
                elif kind == "coldkey_swap":
                    old, new = args
                    if new in owners.values():
                        raise ValueError(f"{new} already owns a hotkey")
                    owners = {
                        hotkey: new if coldkey == old else coldkey
                        for hotkey, coldkey in owners.items()
                    }
                    extrinsics.append(
                        _event(
                            "ColdkeySwapped",
                            {"old_coldkey": old, "new_coldkey": new},
                            phase="ApplyExtrinsic",
                        )
                    )
                elif kind == "noise":
                    (event,) = args
                    if event["phase"] == "Initialization":
                        initialization.append(event)
                    else:
                        extrinsics.append(event)
                    continue
                else:
                    continue
                extrinsics.append(
                    _event(
                        "ExtrinsicSuccess", {}, phase="ApplyExtrinsic", module="System"
                    )
                )
            states[block] = BlockState(
                dict(keys),
                dict(registered),
                dict(weights),
                dict(commits),
                step,
                epoch,
                (*initialization, *extrinsics),
                dict(swap_records),
                code,
            )
        return SourceEmissionChain(states, self.head, interval)


@dataclass(frozen=True)
class StorageKey:
    pallet: str
    name: str
    params: tuple[Any, ...]

    @property
    def value_scale_type(self) -> str:
        return f"{self.pallet}.{self.name}"

    def to_hex(self) -> str:
        return (
            "0x"
            + json.dumps([self.pallet, self.name, list(self.params)]).encode().hex()
        )

    @classmethod
    def from_hex(cls, key: str) -> StorageKey:
        pallet, name, params = json.loads(bytes.fromhex(key[2:]))
        return cls(pallet, name, tuple(params))


def _storage_hash(value: Any) -> str | None:
    if value is None:
        return None
    encoded = json.dumps(value, sort_keys=True).encode()
    return "0x" + hashlib.blake2b(encoded, digest_size=32).hexdigest()


class SourceEmissionChain:
    """Answers the substrate calls of every source-emission path from one model."""

    def __init__(
        self, states: dict[int, BlockState], head: int, swap_interval: int
    ) -> None:
        self._states = states
        self.head = head
        self.swap_interval = swap_interval
        self._numbers = {hash_of(block): block for block in states}

    def state(self, block: int) -> BlockState:
        return self._states[block]

    def _block(self, at: str) -> int:
        if at not in self._numbers:
            raise AssertionError(f"unknown block hash {at}")
        return self._numbers[at]

    def storage(self, pallet: str, name: str, params: Sequence[Any], block: int) -> Any:
        """The value stored under one key after ``block``; None when absent."""
        if (pallet, name) not in _KNOWN_STORAGE:
            raise AssertionError(f"unmodelled storage {pallet}.{name}")
        state = self.state(block)
        if name == "Events":
            assert not params
            return [dict(event) for event in state.events]
        if name == "OwnerHyperparamRateLimit":
            assert not params
            return OWNER_HYPERPARAM_RATE_LIMIT
        netuid, *rest = params
        assert netuid == NETUID, netuid
        if not rest:
            return state.value_storage()[name]
        (key,) = rest
        return state.map_storage()[name].get(key)

    # -- substrate interface -------------------------------------------------

    async def get_chain_finalised_head(self) -> str:
        return hash_of(self.head)

    async def get_block_header(self, *, block_hash: str, **_: Any) -> dict[str, Any]:
        return {"header": {"number": self._block(block_hash)}}

    async def get_block_hash(self, block: int) -> str | None:
        return hash_of(block) if block in self._states else None

    async def get_block_metadata(self, **_: Any) -> Any:
        raise AssertionError("an audited artifact is admitted without metadata")

    async def get_constant(
        self, module_name: str, constant_name: str, block_hash: str, **_: Any
    ) -> int:
        assert (module_name, constant_name) == (
            "SubtensorModule",
            "HotkeySwapOnSubnetInterval",
        )
        self._block(block_hash)
        return self.swap_interval

    async def create_storage_key(
        self, pallet: str, storage_function: str, params: list[Any], **_: Any
    ) -> StorageKey:
        if (pallet, storage_function) not in _KNOWN_STORAGE:
            raise ValueError(f'Storage function "{storage_function}" not found')
        return StorageKey(pallet, storage_function, tuple(params))

    async def decode_scale(self, type_string: str, scale_bytes: bytes, **_: Any) -> Any:
        stored_type, value = json.loads(scale_bytes)
        assert stored_type == type_string, (stored_type, type_string)
        return value

    async def query(
        self,
        module: str,
        storage_function: str,
        params: list[Any] | None = None,
        block_hash: str | None = None,
        **_: Any,
    ) -> Any:
        assert block_hash is not None
        return self.storage(
            module, storage_function, params or [], self._block(block_hash)
        )

    async def query_map(
        self,
        module: str,
        storage_function: str,
        params: list[Any],
        block_hash: str,
        **_: Any,
    ) -> AsyncIterator[tuple[Any, Any]]:
        assert module == "SubtensorModule" and params == [NETUID], params
        rows = sorted(
            self.state(self._block(block_hash)).map_storage()[storage_function].items()
        )

        async def iterate() -> AsyncIterator[tuple[Any, Any]]:
            for row in rows:
                yield row

        return iterate()

    async def rpc_request(self, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "chain_getBlockHash":
            (numbers,) = params
            return {"result": [await self.get_block_hash(n) for n in numbers]}
        if method == "state_getStorageHash":
            key, at = params
            block = self._block(at)
            if key == CODE_KEY:
                return {"result": self.state(block).code}
            storage = StorageKey.from_hex(key)
            value = self.storage(storage.pallet, storage.name, storage.params, block)
            return {"result": _storage_hash(value)}
        if method == "state_queryStorageAt":
            keys, at = params
            block = self._block(at)
            changes = []
            for key in keys:
                storage = StorageKey.from_hex(key)
                value = self.storage(
                    storage.pallet, storage.name, storage.params, block
                )
                encoded = json.dumps([storage.value_scale_type, value]).encode()
                changes.append([key, None if value is None else "0x" + encoded.hex()])
            return {"result": [{"block": at, "changes": changes}]}
        raise AssertionError(f"unmodelled RPC {method}")

    # -- the collector's payout receipt (ChainClient.get_miner_emission_receipt)

    async def get_miner_emission_receipt(
        self,
        netuid: int,
        *,
        payout_block: int,
        allow_initialization_reveals: bool = False,
        **_: Any,
    ) -> ChainMinerEmissionReceipt:
        """The distribution at ``payout_block``, read like the chain client does.

        Vectors are the parent's, unless every write in the block is an
        initialization reveal the full reader attributes; then those rows are
        the post-block vectors that the payout consumed.
        """
        assert netuid == NETUID
        post, parent = self.state(payout_block), self.state(payout_block - 1)
        if post.step != payout_block or parent.step >= payout_block:
            raise ValueError("invalid successful distribution boundary")
        keys = parent.keys
        revealed: set[int] = set()
        if allow_initialization_reveals:
            observed = await verifier.read_source_emission_block(
                self, netuid=netuid, block=payout_block
            )
            if observed.reset_reason or not observed.payout_initialization_reveals:
                raise ChainEmissionReceiptUnavailable(
                    "initialization reveals are not proven before payout"
                )
            hotkeys = {update.validator_hotkey for update in observed.updates}
            revealed = {uid for uid, hotkey in keys.items() if hotkey in hotkeys}
        changed = {
            uid
            for uid in parent.weights.keys() | post.weights.keys()
            if parent.weights.get(uid) != post.weights.get(uid)
        }
        if changed - revealed:
            raise ChainEmissionReceiptUnavailable(
                "weights changed during distribution block"
            )
        consumed = post.weights if revealed else parent.weights
        (emission,) = (
            event["event"]["attributes"]
            for event in post.events
            if event["event_id"] == "IncentiveAlphaEmittedToMiners"
            and event["event"]["attributes"]["netuid"] == NETUID
        )
        return ChainMinerEmissionReceipt(
            NETUID,
            payout_block,
            hash_of(payout_block),
            TIMESTAMP_ZERO + 12 * payout_block,
            post.epoch,
            parent.step,
            OWNER,
            tuple(
                ChainMinerEarning(uid, keys[uid], amount)
                for uid, amount in enumerate(emission["emissions"])
                if amount > 0 and keys[uid] != OWNER
            ),
            tuple(
                ChainWeightVector(
                    uid,
                    keys[uid],
                    tuple(ChainWeight(t, keys[t], w) for t, w in vector),
                )
                for uid, vector in sorted(consumed.items())
            ),
            (),
            (),
        )
