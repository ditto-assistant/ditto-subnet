"""Fail-closed source attribution for an explicitly audited runtime fingerprint.

Semantics audited at RaoFoundation/subtensor 7c732d8d3eb7f8d736bf64d5809c48cbf2e4f028:
coinbase/reveal_commits.rs, coinbase/block_step.rs and epoch/run_epoch.rs.
The caller must configure the deployed WASM code hash independently; a Git
revision is not evidence that the live chain runs that runtime.

TimelockedWeightsRevealed does not include a commit hash. Only a singleton
pending commit plus a unique successful initialization reveal is attributable.
Every other WeightsSet invalidates prior provenance even if weights are equal.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ditto.chain.errors import ChainConnectionError
from ditto.chain.models import ChainMinerEmissionReceipt
from ditto.chain.runtime_interface import (
    metadata_at,
    require_audited_interface,
    runtime_interface,
)

# v475 adds a per-subnet epoch consensus (Yuma or Null). Payout-critical Yuma
# semantics are unchanged through v478 (docs/source-runtime-v475.md); Null
# changes permits, weights and payouts, so these runtimes are audited only while
# SubnetEpochConsensus is Yuma and the reader fails closed otherwise.
_YUMA_GATED_RUNTIME_CODE_HASHES = frozenset(
    {
        "0x557634c8c31bc639ea6552e297dcfb781cd7d9bdd491a5352c151305db33d3a0",
        "0x18b52484d9c5d7523f704c86b17b2ab959fffbc6edb6766a77e9877f0ad19387",
        "0x7e722b3aa5c9192c98665a5d65fa91364bf9055e21fc90efce6d59bf4ef414e1",
    }
)

# https://github.com/RaoFoundation/subtensor/releases/download/v464/subtensor-digest.json
# Compressed/compact WASM for v464 and v459, plus verified compressed
# v466/v467 and v468-v473. Reveal, weights, epoch math and block_step are
# unchanged through v471; v473 only drops a redundant root-coldkey map
# population from block_step. Artifact verification and scoped runtime audits:
# docs/source-runtime-v{466,467,468,475}.md. v472 was an untagged deploy with
# its own artifact (0x43bc67be…), not v471's; it and every later runtime whose
# consumed interface is unchanged are admitted by admit_runtime instead
# (docs/source-runtime-interface.md), so this list no longer has to grow.
AUDITED_RUNTIME_CODE_HASHES = frozenset(
    {
        "0x2f175dcc64196ec8a6b9235f8d7cfd84efef6c68bb925c4455949591cef9f6d2",
        "0xff4ba0da10fb8ac26fab3e446f23413ef7f91de4a604802097ece0b928d53a8e",
        "0x899a87a4e4610587d81d9237adeb3e420ea524cd39b397c7a9b4c811b0e7af1d",
        "0x8858cf3545c90255e5f865dec13713fa1d76cac2be6b8f1070256ee572280abe",
        "0x5675b684d69a07f6f224c2ba9cabef719804911fba40fbe1a2295198c9cb7c47",
        "0x5b0168d2878c1fdcdc424dddca29111b8fe8960bd169742d1c14ce16b22ae381",
        "0x7773f5c0a6d6e9ea9ff347edcc491246eec08a5cf441d964ee96f40d7fa65a08",
        "0x637844a3ad94d3bdbea45664b67bbfa07a31f21c087834a56a772ba27f612b9f",
        "0xd32f5c4347c58f0c5e68dc3e5dd53a26d4e5a24b770fb01279d1b05ec0612218",
        "0x558275958401c026fa4a4159466d49eabd08c761f0c801390593fcba91dee69b",
        "0x148088864116a7bc8bd18af140f522152890e0b7ee33fc979717deb1b48b5e07",
        *_YUMA_GATED_RUNTIME_CODE_HASHES,
    }
)


@dataclass(frozen=True)
class RuntimeAdmission:
    """Why a runtime fingerprint may be read, and what it must still satisfy.

    ``interface_digest`` is set when the runtime was admitted because its
    consumed interface matches an audited runtime's, rather than because its
    WASM artifact was audited (docs/source-runtime-interface.md).
    """

    code_hash: str
    epoch_consensus_gated: bool
    interface_digest: str | None = None


# Keyed by :code hash, which fixes the metadata, so an admission never goes
# stale. Rejections are not cached: a transient metadata failure must retry.
_INTERFACE_ADMISSIONS: dict[str, RuntimeAdmission] = {}


async def admit_runtime(substrate: Any, *, code_hash: Any, at: Any) -> RuntimeAdmission:
    """Admit an audited artifact, or a runtime with an audited interface.

    Runtime upgrades that leave every consumed storage item, event and call
    unchanged are admitted from their own metadata; any change to that surface
    still fails closed and names what moved. A runtime that carries
    ``SubnetEpochConsensus`` is read only while that is Yuma.
    """
    if not isinstance(code_hash, str) or not code_hash.startswith("0x"):
        raise ValueError(f"runtime fingerprint is not audited: {code_hash}")
    if code_hash in AUDITED_RUNTIME_CODE_HASHES:
        return RuntimeAdmission(code_hash, code_hash in _YUMA_GATED_RUNTIME_CODE_HASHES)
    admitted = _INTERFACE_ADMISSIONS.get(code_hash)
    if admitted is not None:
        return admitted
    try:
        metadata = await metadata_at(substrate, at)
    except Exception as error:
        raise ChainConnectionError(
            f"runtime metadata unavailable for {code_hash}"
        ) from error
    try:
        interface = runtime_interface(metadata)
        require_audited_interface(interface)
    except ValueError as error:
        raise ValueError(
            f"runtime fingerprint is not audited: {code_hash} ({error})"
        ) from error
    admitted = RuntimeAdmission(
        code_hash, interface.has_epoch_consensus, interface.digest
    )
    _INTERFACE_ADMISSIONS[code_hash] = admitted
    return admitted


async def _code_hash_at(substrate: Any, at: str) -> Any:
    result = await substrate.rpc_request("state_getStorageHash", ["0x3a636f6465", at])
    return result.get("result") if isinstance(result, dict) else None


async def _epoch_consensus_at(
    substrate: Any, *, netuid: int, at: str, writer: RuntimeAdmission
) -> Any:
    """``SubnetEpochConsensus`` in the state at ``at``.

    ``writer`` is the admission of the runtime stored at ``at``'s parent, the
    one that executed ``at``. The first block that stores an upgrade's code
    was still executed, and its state written, by the previous runtime; the
    new one's migrations run in the next block. A writer without the item
    (before v475) runs Yuma only, and the library decodes that state with the
    writer's metadata, where reading the item fails as "not found".
    """
    if not writer.epoch_consensus_gated:
        return "Yuma"
    return _unwrap(
        await substrate.query(
            module="SubtensorModule",
            storage_function="SubnetEpochConsensus",
            params=[netuid],
            block_hash=at,
        )
    )


@dataclass(frozen=True)
class VotingStake:
    uid: int
    hotkey: str
    stake_u16: int
    active: bool
    permitted: bool


@dataclass(frozen=True)
class RevealedVectorUpdate:
    validator_hotkey: str
    vector_digest: str
    commit_ciphertext_hash: str | None
    commit_block: int | None = None
    reveal_round: int | None = None


@dataclass(frozen=True)
class SourceEmissionBlock:
    block: int
    block_hash: str
    parent_hash: str
    is_payout: bool
    updates: tuple[RevealedVectorUpdate, ...]
    voting_stake: tuple[VotingStake, ...]
    vector_digests: tuple[tuple[str, str], ...]
    runtime_code_hash: str
    reset_reason: str | None = None
    invalidated_hotkeys: tuple[str, ...] = ()
    payout_initialization_reveals: bool = False


@dataclass(frozen=True)
class WinnerBacking:
    """One consumed vector already bound to a verified receipt and winner pin.

    The collector verifies the signed receipt, immutable pin champion and SHA,
    exact committed vector, and absence of any later unbound WeightsSet. These
    are mandatory preconditions, not assertions made by the chain itself.
    """

    validator_hotkey: str
    agent_id: UUID
    artifact_sha256: str
    miner_hotkey: str
    receipt_digest: str
    vector_digest: str


@dataclass(frozen=True)
class WinnerPayoutDecision:
    agent_id: UUID | None
    artifact_sha256: str | None
    miner_hotkey: str | None
    support_lower: int
    other_upper: int
    blocked_reason: str | None


def vector_digest(vector: list[tuple[int, int]] | tuple[tuple[int, int], ...]) -> str:
    """Digest exact stored u16 weights, including zeros, sorted by UID."""
    return hashlib.sha256(
        json.dumps(sorted(vector), separators=(",", ":")).encode()
    ).hexdigest()


def evaluate_winner_payout(
    *,
    receipt: ChainMinerEmissionReceipt,
    stake: tuple[VotingStake, ...],
    backings: tuple[WinnerBacking, ...],
) -> WinnerPayoutDecision:
    """Prove strictly >2/3 effective stake with conservative quantization bounds.

    Runtime StakeWeight stores floor(stake * 65535). q +/- 1 conservatively
    contains each runtime I32F32 stake value. Compare supporting LOWER stake
    with twice ALL OTHER UPPER stake. Unknown evidence and even q=0 validators
    remain in the denominator. No count-based or self-selected quorum exists.
    Exact payout ties are allowed; a merely positive tail payout is not.
    """

    def blocked(reason: str, lower: int = 0, upper: int = 0) -> WinnerPayoutDecision:
        return WinnerPayoutDecision(None, None, None, lower, upper, reason)

    if not stake or len({s.hotkey for s in stake}) != len(stake):
        return blocked("missing_or_duplicate_voting_stake")
    if len({s.uid for s in stake}) != len(stake) or any(
        type(s.stake_u16) is not int or not 0 <= s.stake_u16 <= 65535 for s in stake
    ):
        return blocked("invalid_voting_stake")
    electorate = [s for s in stake if s.active and s.permitted]
    if not electorate or not any(s.stake_u16 for s in electorate):
        return blocked("no_effective_validator_stake")
    if len({b.validator_hotkey for b in backings}) != len(backings):
        return blocked("ambiguous_validator_backing")
    by_hotkey = {b.validator_hotkey: b for b in backings}
    candidates = {(b.agent_id, b.artifact_sha256, b.miner_hotkey) for b in backings}
    earnings = {e.hotkey: e.amount_rao for e in receipt.earnings}
    if not earnings or len(earnings) != len(receipt.earnings):
        return blocked("missing_or_duplicate_miner_payout")
    maximum = max(earnings.values())
    best_lower, best_upper = 0, sum(s.stake_u16 + 1 for s in electorate)
    qualified: list[WinnerPayoutDecision] = []
    for identity in sorted(candidates, key=lambda c: (str(c[0]), c[1], c[2])):
        lower = upper = 0
        for voter in electorate:
            backing = by_hotkey.get(voter.hotkey)
            if (
                backing is not None
                and (backing.agent_id, backing.artifact_sha256, backing.miner_hotkey)
                == identity
            ):
                lower += max(0, voter.stake_u16 - 1)
            else:
                upper += voter.stake_u16 + 1
        if lower > best_lower:
            best_lower, best_upper = lower, upper
        if lower <= 2 * upper:
            continue
        agent_id, sha, hotkey = identity
        if earnings.get(hotkey, 0) <= 0 or earnings[hotkey] != maximum:
            return blocked("winner_did_not_receive_maximal_miner_payout", lower, upper)
        qualified.append(
            WinnerPayoutDecision(agent_id, sha, hotkey, lower, upper, None)
        )
    if len(qualified) != 1:
        return blocked("insufficient_attributed_stake", best_lower, best_upper)
    return qualified[0]


def _uint(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("expected nonnegative integer")
    return value


def _unwrap(value: Any) -> Any:
    return getattr(value, "value", value)


def _attrs(event: dict[str, Any], names: tuple[str, ...]) -> list[Any]:
    nested = event.get("event")
    attrs = nested.get("attributes") if isinstance(nested, dict) else None
    # SCALE tuple events decode to tuples; JSON fixtures/transports use lists.
    # Keep exact arity and named-field validation for either representation.
    if isinstance(attrs, (list, tuple)) and len(attrs) == len(names):
        return list(attrs)
    if isinstance(attrs, dict) and set(attrs) == set(names):
        return [attrs[n] for n in names]
    raise ValueError("unsupported event schema")


def _ciphertext_hash(value: Any) -> str:
    if isinstance(value, str) and value.startswith("0x"):
        data = bytes.fromhex(value[2:])
    elif (
        isinstance(value, (bytes, bytearray))
        or isinstance(value, list)
        and all(type(v) is int and 0 <= v <= 255 for v in value)
    ):
        data = bytes(value)
    else:
        raise ValueError("invalid ciphertext")
    if not data:
        raise ValueError("empty ciphertext")
    return "0x" + hashlib.blake2b(data, digest_size=32).hexdigest()


class SourceEmissionReadCache:
    """Finalized state that consecutive blocks of one scan both read.

    Block N's post-state is block N+1's parent state, and state at a finalized
    block hash never changes, so a cached read is the same fact as a re-read.
    Sharing it halves the archive work per scanned block, which is what the
    rate-limited archives meter. Bounded so a long scan cannot grow it.
    """

    def __init__(self, limit: int = 64) -> None:
        self._values: dict[tuple[Any, ...], Any] = {}
        self._limit = limit

    async def get(
        self, key: tuple[Any, ...], load: Callable[[], Awaitable[Any]]
    ) -> Any:
        if key in self._values:
            return self._values[key]
        value = await load()
        if len(self._values) >= self._limit:
            self._values.pop(next(iter(self._values)))
        self._values[key] = value
        return value


async def read_source_emission_block(
    substrate: Any,
    *,
    netuid: int,
    block: int,
    expected_runtime_code_hash: str | None = None,
    finalized_block: int | None = None,
    cache: SourceEmissionReadCache | None = None,
) -> SourceEmissionBlock:
    """Read a finalized block; unsupported runtime/shape raises, never advances.

    Audited block_step reveals matured commits before running coinbase. A
    same-block payout may consume post-block vectors only when every write has
    one attributable initialization reveal before the unique emission event.
    All other same-block writes remain ambiguous and fail closed.

    A scanner that already read the finalized head passes ``finalized_block``,
    and one ``cache`` across consecutive blocks so each parent state is read
    once. Neither changes what is verified, only how often it is fetched.
    """
    if block < 1:
        raise ValueError("positive block required")
    # A configured fingerprint is not trusted on its own: each observed parent
    # and block hash must equal it and pass admit_runtime below, which admits
    # an interface-matched runtime even on a fresh process's first read.
    if cache is None:
        cache = SourceEmissionReadCache()
    if finalized_block is None:
        finalized_hash = await substrate.get_chain_finalised_head()
        header = await substrate.get_block_header(block_hash=finalized_hash)
        raw_number = header.get("header", header).get("number")
        finalized = (
            int(raw_number, 16) if isinstance(raw_number, str) else _uint(raw_number)
        )
    else:
        finalized = finalized_block
    if block > finalized:
        raise ValueError("block is not finalized")

    async def hash_at(number: int) -> Any:
        return await cache.get(
            ("block_hash", number),
            functools.partial(substrate.get_block_hash, number),
        )

    block_hash, parent_hash = await asyncio.gather(hash_at(block), hash_at(block - 1))
    if not block_hash or not parent_hash:
        raise ValueError("missing historical block")
    code_hashes = await asyncio.gather(
        *(
            cache.get(
                ("code", at),
                functools.partial(
                    substrate.rpc_request,
                    "state_getStorageHash",
                    ["0x3a636f6465", at],
                ),
            )
            for at in (parent_hash, block_hash)
        )
    )
    runtime_hashes = []
    admissions = []
    for code_hash, at in zip(code_hashes, (parent_hash, block_hash), strict=True):
        observed = code_hash.get("result") if isinstance(code_hash, dict) else None
        if (
            expected_runtime_code_hash is not None
            and observed != expected_runtime_code_hash
        ):
            raise ValueError(f"runtime fingerprint is not audited: {observed}")
        admission = await admit_runtime(substrate, code_hash=observed, at=at)
        admissions.append(admission)
        runtime_hashes.append(admission.code_hash)
    if runtime_hashes[0] != runtime_hashes[1]:
        return SourceEmissionBlock(
            block,
            block_hash,
            parent_hash,
            False,
            (),
            (),
            (),
            runtime_hashes[1],
            "runtime_changed",
        )

    async def read(
        name: str, at: str, params: list[Any], module: str = "SubtensorModule"
    ) -> Any:
        return _unwrap(
            await substrate.query(
                module=module, storage_function=name, params=params, block_hash=at
            )
        )

    async def mapping(name: str, at: str) -> list[tuple[Any, Any]]:
        rows = await substrate.query_map(
            module="SubtensorModule",
            storage_function=name,
            params=[netuid],
            block_hash=at,
            fully_exhaust=True,
        )
        return [(_unwrap(k), _unwrap(v)) async for k, v in rows]

    controls = await asyncio.gather(
        *(
            cache.get((netuid, name, at), functools.partial(read, name, at, [netuid]))
            for at in (parent_hash, block_hash)
            for name in ("MechanismCountCurrent", "CommitRevealWeightsEnabled")
        )
    )
    if controls[0] != 1 or controls[2] != 1:
        raise ValueError("unsupported mechanism count")
    if controls[1] is not True or controls[3] is not True:
        raise ValueError("unsupported direct/manual weights mode")
    if admissions[1].epoch_consensus_gated:
        # Each state is gated by the runtime that wrote it: the parent's state
        # by the code stored at the grandparent (see _epoch_consensus_at).
        grandparent = await hash_at(block - 2) if block > 1 else None
        if not grandparent:
            raise ValueError("missing historical block")
        writer_code = await cache.get(
            ("code_hash", grandparent),
            functools.partial(_code_hash_at, substrate, grandparent),
        )
        writers = (
            await admit_runtime(substrate, code_hash=writer_code, at=grandparent),
            admissions[0],
        )
        modes = await asyncio.gather(
            *(
                cache.get(
                    (netuid, "SubnetEpochConsensus", at),
                    functools.partial(
                        _epoch_consensus_at,
                        substrate,
                        netuid=netuid,
                        at=at,
                        writer=writer,
                    ),
                )
                for at, writer in zip((parent_hash, block_hash), writers, strict=True)
            )
        )
        if any(mode != "Yuma" for mode in modes):
            raise ValueError("unsupported epoch consensus")
    key_maps = await asyncio.gather(
        *(
            cache.get((netuid, "Keys", at), functools.partial(mapping, "Keys", at))
            for at in (parent_hash, block_hash)
        )
    )
    keys = dict(key_maps[0])
    if (
        not keys
        or set(keys) != set(range(len(keys)))
        or len(set(keys.values())) != len(keys)
        or len(keys) != len(key_maps[0])
    ):
        raise ValueError("incomplete UID mapping")
    new_keys = dict(key_maps[1])
    if len(new_keys) != len(key_maps[1]) or len(set(new_keys.values())) != len(
        new_keys
    ):
        raise ValueError("invalid new UID mapping")
    if keys != new_keys:
        changed = {
            uid
            for uid in keys.keys() | new_keys.keys()
            if keys.get(uid) != new_keys.get(uid)
        }
        invalidated = {
            h
            for uid in changed
            for h in (keys.get(uid), new_keys.get(uid))
            if h is not None
        }
        old_weights, new_weights, boundary_events = await asyncio.gather(
            mapping("Weights", parent_hash),
            mapping("Weights", block_hash),
            read("Events", block_hash, [], "System"),
        )
        for identities, rows in ((keys, old_weights), (new_keys, new_weights)):
            for uid, vector in rows:
                if uid not in identities or not isinstance(vector, list):
                    raise ValueError("invalid UID transition matrix")
                for pair in vector:
                    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                        raise ValueError("invalid UID transition weight")
                    if _uint(pair[0]) in changed and _uint(pair[1]) > 0:
                        invalidated.add(identities[uid])
        if not isinstance(boundary_events, list):
            raise ValueError("missing UID transition events")
        for event in boundary_events:
            if not isinstance(event, dict):
                raise ValueError("invalid UID transition event")
            if (
                event.get("module_id") == "SubtensorModule"
                and event.get("event_id") == "WeightsSet"
            ):
                subnet, uid = _attrs(event, ("netuid", "uid"))
                if _uint(subnet) == netuid:
                    for identity in (keys.get(_uint(uid)), new_keys.get(_uint(uid))):
                        if identity is not None:
                            invalidated.add(identity)
        return SourceEmissionBlock(
            block,
            block_hash,
            parent_hash,
            False,
            (),
            (),
            (),
            runtime_hashes[1],
            "uid_mapping_changed",
            tuple(sorted(invalidated)),
        )
    events = await read("Events", block_hash, [], "System")
    if not isinstance(events, list):
        raise ValueError("invalid block events")
    writes: dict[int, list[str]] = {}
    reveals: dict[str, list[str]] = {}
    write_positions: dict[int, list[int]] = {}
    reveal_positions: dict[str, list[int]] = {}
    emission_positions: list[int] = []
    for position, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError("invalid event")
        if event.get("module_id") != "SubtensorModule":
            continue
        name = event.get("event_id")
        if name == "WeightsSet":
            subnet, uid = _attrs(event, ("netuid", "uid"))
            if _uint(subnet) == netuid:
                uid = _uint(uid)
                if uid not in keys:
                    raise ValueError("unknown updated validator")
                writes.setdefault(uid, []).append(str(event.get("phase")))
                write_positions.setdefault(uid, []).append(position)
        elif name == "TimelockedWeightsRevealed":
            subnet, who = _attrs(event, ("netuid", "who"))
            if _uint(subnet) == netuid:
                if who not in keys.values():
                    raise ValueError("unknown revealed validator")
                reveals.setdefault(who, []).append(str(event.get("phase")))
                reveal_positions.setdefault(who, []).append(position)
        elif name == "IncentiveAlphaEmittedToMiners":
            subnet, _ = _attrs(event, ("netuid", "emissions"))
            if _uint(subnet) == netuid:
                emission_positions.append(
                    position if event.get("phase") == "Initialization" else -1
                )
    if any(who not in {keys[uid] for uid in writes} for who in reveals):
        raise ValueError("successful reveal without weight write")
    pending: dict[str, list[tuple[str, int, int]]] = {}
    if writes:
        for _, queue in await mapping("TimelockedWeightCommits", parent_hash):
            if not isinstance(queue, (list, tuple)):
                raise ValueError("invalid pending commitments")
            for item in queue:
                if not isinstance(item, (list, tuple)) or len(item) != 4:
                    raise ValueError("invalid pending commitment")
                who, committed, ciphertext, round_number = item
                pending.setdefault(who, []).append(
                    (
                        _ciphertext_hash(ciphertext),
                        _uint(committed),
                        _uint(round_number),
                    )
                )
    is_payout = (
        _uint(await read("LastMechansimStepBlock", block_hash, [netuid])) == block
    )
    post_vectors = (
        dict(await mapping("Weights", block_hash)) if writes or is_payout else {}
    )
    digests = []
    for uid, vector in post_vectors.items():
        if uid not in keys or not isinstance(vector, list):
            raise ValueError("invalid matrix row")
        pairs = [
            (_uint(p[0]), _uint(p[1]))
            for p in vector
            if isinstance(p, (list, tuple)) and len(p) == 2
        ]
        if (
            len(pairs) != len(vector)
            or len({u for u, _ in pairs}) != len(pairs)
            or any(u not in keys or w > 65535 for u, w in pairs)
        ):
            raise ValueError("invalid matrix weights")
        digests.append((keys[uid], vector_digest(pairs)))

    updates = []
    for uid, phases in writes.items():
        vector = post_vectors.get(uid)
        if not isinstance(vector, list) or any(
            not isinstance(p, (tuple, list)) or len(p) != 2 for p in vector
        ):
            raise ValueError("missing updated vector")
        pairs = [(_uint(p[0]), _uint(p[1])) for p in vector]
        if len({u for u, _ in pairs}) != len(pairs) or any(
            u not in keys or w > 65535 for u, w in pairs
        ):
            raise ValueError("invalid updated vector")
        who = keys[uid]
        commits = pending.get(who, [])
        proof = (
            commits[0]
            if len(commits) == 1
            and phases == ["Initialization"]
            and reveals.get(who) == ["Initialization"]
            else (None, None, None)
        )
        updates.append(RevealedVectorUpdate(who, vector_digest(pairs), *proof))

    payout_initialization_reveals = bool(
        is_payout
        and updates
        and len(emission_positions) == 1
        and all(change.commit_ciphertext_hash is not None for change in updates)
        and all(
            write_positions[uid][0]
            < reveal_positions[keys[uid]][0]
            < emission_positions[0]
            for uid in writes
        )
    )

    voting = []
    if is_payout:
        scores = await read("StakeWeight", block_hash, [netuid])
        active = await read("Active", block_hash, [netuid])
        permits = await read("ValidatorPermit", parent_hash, [netuid])
        owner = await read("SubnetOwnerHotkey", parent_hash, [netuid])
        if any(
            not isinstance(v, list) or len(v) != len(keys)
            for v in (scores, active, permits)
        ):
            raise ValueError("incomplete payout voting state")
        for uid, who in keys.items():
            q = _uint(scores[uid])
            if (
                q > 65535
                or type(active[uid]) is not bool
                or type(permits[uid]) is not bool
            ):
                raise ValueError("invalid payout voting state")
            voting.append(
                VotingStake(uid, who, q, active[uid], permits[uid] or who == owner)
            )
    return SourceEmissionBlock(
        block,
        block_hash,
        parent_hash,
        is_payout,
        tuple(updates),
        tuple(voting),
        tuple(digests),
        runtime_hashes[1],
        payout_initialization_reveals=payout_initialization_reveals,
    )


# SubtensorModule events that can move identities or subnets. The endpoint Keys
# comparison already routes a net change through the full scan; these names only
# add full reads (never skips), so matching more than needed is harmless.
# Matched case-insensitively ("regist" covers NeuronRegistered and
# NeuronDeregistered; "prun" covers explicit UID pruning).
_STRUCTURAL_EVENT_MARKERS = (
    "swap",
    "regist",
    "dissolv",
    "networkremoved",
    "prun",
    "epochconsensus",
)
_FILTERED_WRITE_EVENTS = {
    "WeightsSet": ("netuid", "uid"),
    "TimelockedWeightsRevealed": ("netuid", "who"),
    "IncentiveAlphaEmittedToMiners": ("netuid", "emissions"),
}


@dataclass(frozen=True)
class SourceEmissionRangePlan:
    """Which blocks of a finalized range need the full per-block reader.

    ``hashes`` covers ``first_block - 1`` through ``last_block``. With
    ``full_scan`` the caller reads every block as before; otherwise only
    ``interesting`` blocks are read and every other block is provably quiet:
    ``read_source_emission_block`` would return no payout, no vector update and
    no reset for it, so committing it changes nothing but the cursor.
    """

    first_block: int
    last_block: int
    hashes: dict[int, str]
    interesting: tuple[int, ...]
    full_scan: bool
    runtime_code_hash: str | None


def _valid_uid_mapping(rows: list[tuple[Any, Any]]) -> dict[int, str] | None:
    keys = dict(rows)
    if (
        not keys
        or len(keys) != len(rows)
        or set(keys) != set(range(len(keys)))
        or len(set(keys.values())) != len(keys)
    ):
        return None
    return keys


def _quiet_block_events(events: Any, netuid: int) -> bool:
    """True only when no event can change what the per-block reader returns."""
    if not isinstance(events, list):
        return False
    for event in events:
        if not isinstance(event, dict):
            return False
        module, name = event.get("module_id"), event.get("event_id")
        if not isinstance(name, str):
            return False
        if module == "System" and name == "CodeUpdated":
            return False
        if module != "SubtensorModule":
            continue
        if name in _FILTERED_WRITE_EVENTS:
            try:
                subnet = _uint(_attrs(event, _FILTERED_WRITE_EVENTS[name])[0])
            except ValueError:
                # Let the full reader apply its own fail-closed validation.
                return False
            if subnet == netuid:
                return False
        elif any(marker in name.lower() for marker in _STRUCTURAL_EVENT_MARKERS):
            return False
    return True


@dataclass(frozen=True)
class _RangeEndpoints:
    """A range whose endpoint invariants hold, ready for a quiet-block filter."""

    first_block: int
    last_block: int
    hashes: dict[int, str]
    runtime_code_hash: str
    # Blocks that changed a UID's hotkey inside the range. Always read in
    # full: the reader resets provenance there (uid_mapping_changed).
    identity_changes: tuple[int, ...] = ()

    def plan(self, interesting: Iterable[int]) -> SourceEmissionRangePlan:
        return SourceEmissionRangePlan(
            self.first_block,
            self.last_block,
            self.hashes,
            tuple(sorted({*interesting, *self.identity_changes})),
            False,
            self.runtime_code_hash,
        )


# More changed UIDs than this in one range: plan the range in full instead.
_MAX_IDENTITY_CHANGES = 16


async def _gather_or_cancel(*coroutines: Awaitable[Any]) -> list[Any]:
    """``asyncio.gather``, but a failure cancels and awaits the siblings."""
    tasks = [asyncio.ensure_future(c) for c in coroutines]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _storage_change_blocks(
    substrate: Any,
    key_hex: str,
    hashes: dict[int, str],
    low: int,
    high: int,
    gate: asyncio.Semaphore,
) -> list[int]:
    """Blocks in ``(low, high]`` where ``key_hex``'s storage hash changed.

    Exact only for a value that never returns to an earlier one inside the
    interval (bisection cannot see A -> B -> A); callers argue that per key.
    """
    memo: dict[int, Any] = {}

    async def at(block: int) -> Any:
        if block not in memo:
            async with gate:
                result = await substrate.rpc_request(
                    "state_getStorageHash", [key_hex, hashes[block]]
                )
            # ``result: null`` is absent storage, a valid answer; a response
            # without it is not, and must not read as "unchanged".
            if not isinstance(result, dict) or "result" not in result:
                raise ValueError(f"missing storage hash at block {block}")
            memo[block] = result["result"]
        return memo[block]

    async def changes(lo: int, hi: int) -> list[int]:
        lo_hash, hi_hash = await _gather_or_cancel(at(lo), at(hi))
        if lo_hash == hi_hash:
            return []
        if hi - lo == 1:
            return [hi]
        mid = (lo + hi) // 2
        left, right = await _gather_or_cancel(changes(lo, mid), changes(mid, hi))
        return left + right

    return await changes(low, high)


async def _range_endpoints(
    substrate: Any, *, netuid: int, first_block: int, last_block: int
) -> _RangeEndpoints | SourceEmissionRangePlan:
    """Shared endpoint checks; a ``full_scan`` plan when any of them fails.

    If the runtime code or the mode controls differ between ``first_block - 1``
    and ``last_block``, or any of them (or the ``Keys`` map) is invalid at
    either end, the caller must run the unchanged per-block scan. A runtime
    upgrade cannot revert inside a range (spec versions only increase). A UID
    whose hotkey or ``BlockAtRegistration`` differs at the ends is located by
    bisection and its blocks are returned as ``identity_changes``; see the
    comment below for why a UID equal on both at the ends was not touched.
    """
    if not 0 < first_block <= last_block:
        raise ValueError("invalid source-emission range")
    numbers = list(range(first_block - 1, last_block + 1))
    response = await substrate.rpc_request("chain_getBlockHash", [numbers])
    raw_hashes = response.get("result") if isinstance(response, dict) else None
    if (
        not isinstance(raw_hashes, list)
        or len(raw_hashes) != len(numbers)
        or not all(isinstance(h, str) and h.startswith("0x") for h in raw_hashes)
    ):
        raise ValueError("missing historical block hashes")
    hashes = dict(zip(numbers, raw_hashes, strict=True))
    start, end = hashes[first_block - 1], hashes[last_block]

    def full(code_hash: str | None = None) -> SourceEmissionRangePlan:
        return SourceEmissionRangePlan(
            first_block, last_block, hashes, (), True, code_hash
        )

    async def read(name: str, at: str) -> Any:
        return _unwrap(
            await substrate.query(
                module="SubtensorModule",
                storage_function=name,
                params=[netuid],
                block_hash=at,
            )
        )

    async def mapping_at(name: str, at: str) -> list[tuple[Any, Any]]:
        rows = await substrate.query_map(
            module="SubtensorModule",
            storage_function=name,
            params=[netuid],
            block_hash=at,
            fully_exhaust=True,
        )
        return [(_unwrap(k), _unwrap(v)) async for k, v in rows]

    async def keys_at(at: str) -> list[tuple[Any, Any]]:
        return await mapping_at("Keys", at)

    code = []
    for at in (start, end):
        result = await substrate.rpc_request(
            "state_getStorageHash", ["0x3a636f6465", at]
        )
        code.append(result.get("result") if isinstance(result, dict) else None)
    if code[0] != code[1]:
        return full()
    try:
        admission = await admit_runtime(substrate, code_hash=code[0], at=start)
    except (ValueError, ChainConnectionError):
        # The full scan admits the same fingerprint and reports why it stopped.
        return full()
    # The end's state was written by the range's runtime; the start's by the
    # code stored before it (see _epoch_consensus_at).
    writers = {end: admission}
    if admission.epoch_consensus_gated:
        if first_block < 2:
            return full(code[0])
        before = await substrate.rpc_request("chain_getBlockHash", [[first_block - 2]])
        rows = before.get("result") if isinstance(before, dict) else None
        before_hash = rows[0] if isinstance(rows, list) and len(rows) == 1 else None
        if not isinstance(before_hash, str):
            return full(code[0])
        try:
            writers[start] = await admit_runtime(
                substrate,
                code_hash=await _code_hash_at(substrate, before_hash),
                at=before_hash,
            )
        except (ValueError, ChainConnectionError):
            return full(code[0])
    for at in (start, end):
        if await read("MechanismCountCurrent", at) != 1:
            return full(code[0])
        if await read("CommitRevealWeightsEnabled", at) is not True:
            return full(code[0])
        if (
            admission.epoch_consensus_gated
            and await _epoch_consensus_at(
                substrate, netuid=netuid, at=at, writer=writers[at]
            )
            != "Yuma"
        ):
            return full(code[0])
    start_keys, end_keys = await keys_at(start), await keys_at(end)
    start_map, end_map = _valid_uid_mapping(start_keys), _valid_uid_mapping(end_keys)
    if start_map is None or end_map is None or set(start_map) != set(end_map):
        return full(code[0])
    # UIDs re-registered inside the range, found by BlockAtRegistration: every
    # registration sets it to the current block, so per UID it only increases
    # and never repeats. That catches A -> B -> A, which equal Keys at the
    # ends would hide (SN118's 300-block immunity is shorter than a range),
    # and bisecting it finds every registration exactly. Keys is bisected too
    # for a hotkey swap, which registers nothing; a UID's hotkey can be
    # swapped at most once per HotkeySwapOnSubnetInterval (a day), so its
    # value cannot flip back inside one range either.
    registered = await _gather_or_cancel(
        *(mapping_at("BlockAtRegistration", at) for at in (start, end))
    )
    if any(set(dict(rows)) != set(start_map) for rows in registered):
        return full(code[0])
    first_reg, last_reg = (dict(rows) for rows in registered)
    changed = sorted(
        uid
        for uid in start_map
        if start_map[uid] != end_map[uid] or first_reg[uid] != last_reg[uid]
    )
    if len(changed) > _MAX_IDENTITY_CHANGES:
        return full(code[0])
    gate = asyncio.Semaphore(8)

    async def bisect(uid: int, storage: str) -> list[int]:
        key = await substrate.create_storage_key(
            "SubtensorModule", storage, [netuid, uid], block_hash=end
        )
        return await _storage_change_blocks(
            substrate, key.to_hex(), hashes, first_block - 1, last_block, gate
        )

    found = await _gather_or_cancel(
        *(
            bisect(uid, storage)
            for uid in changed
            for storage in ("BlockAtRegistration", "Keys")
        )
    )
    identity_changes: set[int] = set()
    for registrations, swaps in zip(found[::2], found[1::2], strict=True):
        blocks = {*registrations, *swaps}
        if not blocks:
            return full(code[0])  # the endpoints disagree but no change found
        identity_changes.update(blocks)
    return _RangeEndpoints(
        first_block,
        last_block,
        hashes,
        admission.code_hash,
        tuple(sorted(identity_changes)),
    )


async def plan_source_emission_range(
    substrate: Any,
    *,
    netuid: int,
    first_block: int,
    last_block: int,
    concurrency: int = 8,
) -> SourceEmissionRangePlan:
    """Find the blocks of ``[first_block, last_block]`` that need a full read.

    After the shared endpoint checks (``_range_endpoints``), a code change and
    revert inside the range is caught by ``System.CodeUpdated`` and an identity
    flip-flop by the structural event markers. Each block's events and payout
    marker are then read with one ``state_queryStorageAt`` call.
    """
    endpoints = await _range_endpoints(
        substrate, netuid=netuid, first_block=first_block, last_block=last_block
    )
    if isinstance(endpoints, SourceEmissionRangePlan):
        return endpoints
    hashes = endpoints.hashes
    end = hashes[last_block]

    events_key = await substrate.create_storage_key(
        "System", "Events", [], block_hash=end
    )
    step_key = await substrate.create_storage_key(
        "SubtensorModule", "LastMechansimStepBlock", [netuid], block_hash=end
    )
    events_hex, step_hex = events_key.to_hex(), step_key.to_hex()
    gate = asyncio.Semaphore(concurrency)

    async def interesting(block: int) -> bool:
        at = hashes[block]
        async with gate:
            result = await substrate.rpc_request(
                "state_queryStorageAt", [[events_hex, step_hex], at]
            )
        sets = result.get("result") if isinstance(result, dict) else None
        if not isinstance(sets, list) or len(sets) != 1:
            raise ValueError("invalid storage read")
        changes = dict(sets[0].get("changes") or [])
        raw_events, raw_step = changes.get(events_hex), changes.get(step_hex)
        if not isinstance(raw_events, str) or not isinstance(raw_step, str):
            # A missing payout marker or events value is not provably quiet.
            return True
        step = _unwrap(
            await substrate.decode_scale(
                step_key.value_scale_type, bytes.fromhex(raw_step[2:]), block_hash=at
            )
        )
        if _uint(step) == block:
            return True
        events = _unwrap(
            await substrate.decode_scale(
                events_key.value_scale_type,
                bytes.fromhex(raw_events[2:]),
                block_hash=at,
            )
        )
        return not _quiet_block_events(events, netuid)

    blocks = list(range(first_block, last_block + 1))
    tasks = [asyncio.create_task(interesting(block)) for block in blocks]
    try:
        flags = await asyncio.gather(*tasks)
    except BaseException:
        # Stop sibling reads before the caller falls back to its own scan.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return endpoints.plan(
        block for block, flag in zip(blocks, flags, strict=True) if flag
    )


async def plan_source_emission_range_by_reveals(
    substrate: Any,
    *,
    netuid: int,
    first_block: int,
    last_block: int,
    concurrency: int = 8,
) -> SourceEmissionRangePlan:
    """Plan a range from payouts and reveal-queue changes, without event reads.

    Under the shared endpoint checks, the only blocks the full reader can act
    on are payouts and SN118 weight writes. With commit-reveal enabled every
    write comes from a commit-reveal v3 reveal (direct ``set_weights`` is
    rejected) or a v2 ``reveal_weights``, refused here by requiring an empty
    v2 commit store at every probed block.

    * **Payouts** are found by hopping ``LastMechansimStepBlock`` back from
      the range end, one read per payout.
    * **Reveals** pop entries off ``TimelockedWeightCommits(netuid, e)``, where
      ``e`` is the epoch before the current one. Within one epoch that key
      only shrinks (commits go to the current epoch's key, and a taken entry
      never comes back), so bisecting its storage hash between payouts finds
      every block that revealed, exactly and with tiny reads.

    Any other shape (epoch moving without a payout, a changed reveal period,
    controls changing between payouts) returns the full-scan plan. Shadowed
    against ``plan_source_emission_range`` before it is trusted: a reveal
    path this misses shows up there as a disagreement.
    """
    endpoints = await _range_endpoints(
        substrate, netuid=netuid, first_block=first_block, last_block=last_block
    )
    if isinstance(endpoints, SourceEmissionRangePlan):
        return endpoints
    hashes = endpoints.hashes
    end = hashes[last_block]
    gate = asyncio.Semaphore(concurrency)

    def full() -> SourceEmissionRangePlan:
        return SourceEmissionRangePlan(
            first_block, last_block, hashes, (), True, endpoints.runtime_code_hash
        )

    async def read(name: str, block: int) -> Any:
        async with gate:
            return _unwrap(
                await substrate.query(
                    module="SubtensorModule",
                    storage_function=name,
                    params=[netuid],
                    block_hash=hashes[block],
                )
            )

    async def v2_commits(block: int) -> int:
        async with gate:
            rows = await substrate.query_map(
                module="SubtensorModule",
                storage_function="WeightCommits",
                params=[netuid],
                block_hash=hashes[block],
                fully_exhaust=True,
            )
            return len([row async for row in rows])

    reveal_periods = {
        _uint(await read("RevealPeriodEpochs", block))
        for block in (first_block - 1, last_block)
    }
    if len(reveal_periods) != 1 or 0 in reveal_periods:
        return full()
    reveal_period = reveal_periods.pop()

    # The controls are read at the ends and at every payout, not every block.
    # A subnet owner cannot switch commit-reveal or the mechanism count off and
    # back on between them: each change is rate-limited to one per
    # tempo * OwnerHyperparamRateLimit blocks, longer than the range. Only root
    # can, and root can also change that limit, so it must hold at both ends.
    async def owner_change_interval(block: int) -> int:
        async with gate:
            limit = _unwrap(
                await substrate.query(
                    module="SubtensorModule",
                    storage_function="OwnerHyperparamRateLimit",
                    params=[],
                    block_hash=hashes[block],
                )
            )
        return _uint(await read("Tempo", block)) * _uint(limit)

    intervals = {
        await owner_change_interval(block) for block in (first_block - 1, last_block)
    }
    if len(intervals) != 1 or intervals.pop() <= last_block - first_block:
        return full()

    # Payouts, newest first: each payout's parent state names the previous one.
    payouts: list[int] = []
    step = _uint(await read("LastMechansimStepBlock", last_block))
    while step >= first_block:
        payouts.append(step)
        previous = _uint(await read("LastMechansimStepBlock", step - 1))
        if previous >= step:
            raise ValueError("payout markers do not move backwards")
        step = previous
    payouts.reverse()

    # One segment per epoch: (state before, last block, epoch it reveals for).
    starts = [first_block - 1, *payouts]
    segments: list[tuple[int, int, int]] = []
    for index, start in enumerate(starts):
        last = payouts[index] - 1 if index < len(payouts) else last_block
        epoch = _uint(await read("SubnetEpochIndex", start))
        if _uint(await read("SubnetEpochIndex", last)) != epoch:
            return full()  # the epoch moved without a payout marker
        if epoch < reveal_period:
            return full()
        # A payout's own block reveals too, so its segment starts before it.
        before = start if index == 0 else start - 1
        segments.append((before, last, epoch - reveal_period))
    for block in {first_block - 1, last_block, *payouts}:
        if await read("CommitRevealWeightsEnabled", block) is not True:
            return full()
        if await read("MechanismCountCurrent", block) != 1:
            return full()
        if await v2_commits(block):
            return full()

    interesting = set(payouts)
    for before, last, reveal_epoch in segments:
        key = await substrate.create_storage_key(
            "SubtensorModule",
            "TimelockedWeightCommits",
            [netuid, reveal_epoch],
            block_hash=end,
        )
        interesting.update(
            await _storage_change_blocks(
                substrate, key.to_hex(), hashes, before, last, gate
            )
        )
    return endpoints.plan(interesting)


async def verify_finalized_weight_commit(substrate: Any, receipt: Any) -> None:
    """Verify direct CR commit inclusion against canonical finalized chain data.

    Receipt version_key lives inside the encrypted payload, not commit call
    arguments. It cannot be separately read here: successful reveal checks the
    runtime's version gate, and the collector verifies the resulting vector.
    Unsupported proxy/batch calls fail closed rather than guessing origin.
    Missing provider data raises ChainConnectionError for archive fallback;
    complete data disproving a claim raises ValueError for per-validator isolation.
    """
    attempt = receipt.attempt
    finalized_hash = await substrate.get_chain_finalised_head()
    header = await substrate.get_block_header(block_hash=finalized_hash)
    if not finalized_hash or not isinstance(header, dict):
        raise ChainConnectionError("missing commit finality header")
    payload = header.get("header", header)
    if not isinstance(payload, dict) or payload.get("number") is None:
        raise ChainConnectionError("missing commit finality height")
    number = payload["number"]
    finalized = int(number, 16) if isinstance(number, str) else _uint(number)
    if attempt.commit_block > finalized:
        raise ChainConnectionError("provider has not finalized commit block")
    canonical = await substrate.get_block_hash(attempt.commit_block)
    if not canonical:
        raise ChainConnectionError("missing canonical commit block")
    if canonical != attempt.commit_block_hash:
        raise ValueError("commit block is not canonical")

    async def admitted_at(at: Any) -> RuntimeAdmission:
        if not at:
            raise ChainConnectionError("missing commit parent block")
        code = await substrate.rpc_request("state_getStorageHash", ["0x3a636f6465", at])
        if not isinstance(code, dict) or not code.get("result"):
            raise ChainConnectionError("missing historical commit runtime")
        try:
            return await admit_runtime(substrate, code_hash=code["result"], at=at)
        except ValueError as error:
            raise ValueError("commit runtime is not audited") from error

    parent = await substrate.get_block_hash(attempt.commit_block - 1)
    parent_admission = await admitted_at(parent)
    canonical_admission = await admitted_at(canonical)
    for at, admission in ((parent, parent_admission), (canonical, canonical_admission)):
        if not admission.epoch_consensus_gated:
            continue
        # Each state is gated by the runtime that wrote it, the one stored at
        # its parent (_epoch_consensus_at).
        writer = (
            parent_admission
            if at == canonical
            else await admitted_at(
                await substrate.get_block_hash(attempt.commit_block - 2)
            )
        )
        # Null consensus queues commits differently; only Yuma is audited.
        mode = await _epoch_consensus_at(
            substrate, netuid=receipt.netuid, at=at, writer=writer
        )
        if mode is None:
            raise ChainConnectionError("missing historical epoch consensus")
        if mode != "Yuma":
            raise ValueError("commit epoch consensus is not audited")
    raw, decoded, events = await asyncio.gather(
        substrate.rpc_request("chain_getBlock", [canonical]),
        substrate.get_block(block_hash=canonical),
        substrate.query(
            module="System", storage_function="Events", params=[], block_hash=canonical
        ),
    )
    raw_result = raw.get("result") if isinstance(raw, dict) else None
    raw_block = raw_result.get("block") if isinstance(raw_result, dict) else None
    raw_extrinsics = (
        raw_block.get("extrinsics") if isinstance(raw_block, dict) else None
    )
    decoded_extrinsics = (
        decoded.get("extrinsics") if isinstance(decoded, dict) else None
    )
    index = attempt.extrinsic_index
    if (
        not isinstance(raw_extrinsics, list)
        or not isinstance(decoded_extrinsics, list)
        or len(raw_extrinsics) != len(decoded_extrinsics)
    ):
        raise ChainConnectionError("missing historical commit extrinsics")
    if not 0 <= index < len(raw_extrinsics):
        raise ValueError("commit extrinsic index is outside canonical block")
    encoded = raw_extrinsics[index]
    if not isinstance(encoded, str) or not encoded.startswith("0x"):
        raise ChainConnectionError("invalid raw extrinsic response")
    extrinsic_hash = (
        "0x" + hashlib.blake2b(bytes.fromhex(encoded[2:]), digest_size=32).hexdigest()
    )
    if extrinsic_hash != attempt.extrinsic_hash:
        raise ValueError("commit extrinsic hash mismatch")
    extrinsic = _unwrap(decoded_extrinsics[index])
    if (
        not isinstance(extrinsic, dict)
        or extrinsic.get("address") != receipt.validator_hotkey
    ):
        raise ValueError("commit signer mismatch")
    call = extrinsic.get("call")
    if not isinstance(call, dict) or call.get("call_module") != "SubtensorModule":
        raise ValueError("unsupported commit call module")
    method = call.get("call_function")
    if method not in (
        "commit_timelocked_weights",
        "commit_timelocked_mechanism_weights",
    ):
        raise ValueError("unsupported commit call")
    raw_args = call.get("call_args")
    if not isinstance(raw_args, list) or any(
        not isinstance(a, dict) or "name" not in a or "value" not in a for a in raw_args
    ):
        raise ValueError("invalid commit arguments")
    args = {a["name"]: a["value"] for a in raw_args}
    if len(args) != len(raw_args):
        raise ValueError("duplicate commit argument")
    if (
        args.get("netuid") != receipt.netuid
        or args.get("mecid", 0) != receipt.mechanism_id
        or args.get("reveal_round") != attempt.reveal_round
    ):
        raise ValueError("commit subnet, mechanism or round mismatch")
    actual_hash = _ciphertext_hash(args.get("commit"))
    if actual_hash.removeprefix("0x") != attempt.ciphertext_hash:
        raise ValueError("commit ciphertext mismatch")
    events = _unwrap(events)
    if not isinstance(events, list):
        raise ChainConnectionError("missing historical commit events")
    success = failed = committed = 0
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("invalid commit event")
        if (
            event.get("phase") != "ApplyExtrinsic"
            or event.get("extrinsic_idx") != index
        ):
            continue
        if event.get("module_id") == "System":
            success += event.get("event_id") == "ExtrinsicSuccess"
            failed += event.get("event_id") == "ExtrinsicFailed"
        if (
            event.get("module_id") == "SubtensorModule"
            and event.get("event_id") == "TimelockedWeightsCommitted"
        ):
            who, netuid, digest, round_number = _attrs(
                event, ("who", "netuid", "commit_hash", "reveal_round")
            )
            if (
                who != receipt.validator_hotkey
                or netuid != receipt.netuid
                or digest != actual_hash
                or round_number != attempt.reveal_round
            ):
                raise ValueError("commit event identity mismatch")
            committed += 1
    if success != 1 or failed or committed != 1:
        raise ValueError("commit lacks unique successful inclusion evidence")
