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
import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ditto.chain.errors import ChainConnectionError
from ditto.chain.models import ChainMinerEmissionReceipt

# https://github.com/RaoFoundation/subtensor/releases/download/v464/subtensor-digest.json
# Compressed/compact WASM for v464 and v459. Reveal, epoch math and block_step
# are identical; v464 changes unrelated fees, owner leases and root dividends.
AUDITED_RUNTIME_CODE_HASHES = frozenset(
    {
        "0x637844a3ad94d3bdbea45664b67bbfa07a31f21c087834a56a772ba27f612b9f",
        "0xd32f5c4347c58f0c5e68dc3e5dd53a26d4e5a24b770fb01279d1b05ec0612218",
        "0x558275958401c026fa4a4159466d49eabd08c761f0c801390593fcba91dee69b",
        "0x148088864116a7bc8bd18af140f522152890e0b7ee33fc979717deb1b48b5e07",
    }
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
    if isinstance(attrs, list) and len(attrs) == len(names):
        return attrs
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


async def read_source_emission_block(
    substrate: Any,
    *,
    netuid: int,
    block: int,
    expected_runtime_code_hash: str | None = None,
) -> SourceEmissionBlock:
    """Read a finalized block; unsupported runtime/shape raises, never advances.

    A payout whose WeightsSet occurs in the same block must not be attributed
    using the returned post-block updates. Process payout first against the
    prior durable bindings and reject if any target-subnet WeightsSet exists;
    only then apply updates for future payouts.
    """
    if block < 1:
        raise ValueError("positive block required")
    if (
        expected_runtime_code_hash is not None
        and expected_runtime_code_hash not in AUDITED_RUNTIME_CODE_HASHES
    ):
        raise ValueError("configured runtime fingerprint is not audited")
    finalized_hash = await substrate.get_chain_finalised_head()
    header = await substrate.get_block_header(block_hash=finalized_hash)
    raw_number = header.get("header", header).get("number")
    finalized = (
        int(raw_number, 16) if isinstance(raw_number, str) else _uint(raw_number)
    )
    if block > finalized:
        raise ValueError("block is not finalized")
    block_hash, parent_hash = await asyncio.gather(
        substrate.get_block_hash(block), substrate.get_block_hash(block - 1)
    )
    if not block_hash or not parent_hash:
        raise ValueError("missing historical block")
    code_hashes = await asyncio.gather(
        *(
            substrate.rpc_request("state_getStorageHash", ["0x3a636f6465", at])
            for at in (parent_hash, block_hash)
        )
    )
    runtime_hashes = []
    for code_hash in code_hashes:
        observed = code_hash.get("result") if isinstance(code_hash, dict) else None
        if observed not in AUDITED_RUNTIME_CODE_HASHES or (
            expected_runtime_code_hash is not None
            and observed != expected_runtime_code_hash
        ):
            raise ValueError(f"runtime fingerprint is not audited: {observed}")
        runtime_hashes.append(observed)
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
            read(name, at, [netuid])
            for at in (parent_hash, block_hash)
            for name in ("MechanismCountCurrent", "CommitRevealWeightsEnabled")
        )
    )
    if controls[0] != 1 or controls[2] != 1:
        raise ValueError("unsupported mechanism count")
    if controls[1] is not True or controls[3] is not True:
        raise ValueError("unsupported direct/manual weights mode")
    key_maps = await asyncio.gather(
        mapping("Keys", parent_hash), mapping("Keys", block_hash)
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
    for event in events:
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
        elif name == "TimelockedWeightsRevealed":
            subnet, who = _attrs(event, ("netuid", "who"))
            if _uint(subnet) == netuid:
                if who not in keys.values():
                    raise ValueError("unknown revealed validator")
                reveals.setdefault(who, []).append(str(event.get("phase")))
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
    )


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
    for at in (await substrate.get_block_hash(attempt.commit_block - 1), canonical):
        if not at:
            raise ChainConnectionError("missing commit parent block")
        code = await substrate.rpc_request("state_getStorageHash", ["0x3a636f6465", at])
        if not isinstance(code, dict) or not code.get("result"):
            raise ChainConnectionError("missing historical commit runtime")
        if code["result"] not in AUDITED_RUNTIME_CODE_HASHES:
            raise ValueError("commit runtime is not audited")
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
