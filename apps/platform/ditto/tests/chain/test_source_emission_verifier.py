"""Commit identity, effective-stake bounds and completed payout controls."""

from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from ditto.chain.models import ChainMinerEarning, ChainMinerEmissionReceipt
from ditto.chain.source_emission_verifier import (
    AUDITED_RUNTIME_CODE_HASHES,
    VotingStake,
    WinnerBacking,
    evaluate_winner_payout,
    read_source_emission_block,
    vector_digest,
)

A = UUID(int=1)
B = UUID(int=2)


def receipt() -> ChainMinerEmissionReceipt:
    return ChainMinerEmissionReceipt(
        118,
        100,
        "h100",
        1200,
        10,
        80,
        "owner",
        (ChainMinerEarning(1, "miner", 65), ChainMinerEarning(2, "tail", 35)),
        (),
        (),
        (),
    )


def backing(hotkey: str, agent: UUID = A) -> WinnerBacking:
    return WinnerBacking(
        hotkey, agent, str(agent), "miner", f"receipt-{agent}", "vector"
    )


def test_unknown_stake_is_in_denominator_and_strict_boundary() -> None:
    for support, allowed in [(43690, False), (43691, False), (44000, True)]:
        stake = (
            VotingStake(0, "v1", support, True, True),
            VotingStake(1, "v2", 65535 - support, True, True),
        )
        decision = evaluate_winner_payout(
            receipt=receipt(), stake=stake, backings=(backing("v1"),)
        )
        assert (decision.agent_id == A) is allowed
    # Zero-quantized active stake remains possible positive stake.
    decision = evaluate_winner_payout(
        receipt=receipt(),
        stake=(
            VotingStake(0, "v1", 3, True, True),
            VotingStake(1, "v2", 0, True, True),
        ),
        backings=(backing("v1"),),
    )
    assert decision.agent_id is None
    assert decision.other_upper == 1


def test_same_hotkey_submissions_cannot_share_one_payout_or_vote() -> None:
    stake = (
        VotingStake(0, "v1", 40000, True, True),
        VotingStake(1, "v2", 25535, True, True),
    )
    split = evaluate_winner_payout(
        receipt=receipt(), stake=stake, backings=(backing("v1", A), backing("v2", B))
    )
    assert split.agent_id is None
    duplicate = evaluate_winner_payout(
        receipt=receipt(), stake=stake, backings=(backing("v1", A), backing("v1", B))
    )
    assert duplicate.blocked_reason == "ambiguous_validator_backing"


def test_tail_only_rejected_tied_winner_allowed() -> None:
    stake = (VotingStake(0, "v1", 65535, True, True),)
    tied = replace(
        receipt(),
        earnings=(ChainMinerEarning(1, "miner", 50), ChainMinerEarning(2, "tail", 50)),
    )
    assert (
        evaluate_winner_payout(
            receipt=tied, stake=stake, backings=(backing("v1"),)
        ).agent_id
        == A
    )
    tail = replace(
        receipt(),
        earnings=(ChainMinerEarning(1, "miner", 10), ChainMinerEarning(2, "tail", 90)),
    )
    assert (
        evaluate_winner_payout(
            receipt=tail, stake=stake, backings=(backing("v1"),)
        ).blocked_reason
        == "winner_did_not_receive_maximal_miner_payout"
    )


def test_inactive_nonpermitted_stake_does_not_vote() -> None:
    stake = (
        VotingStake(0, "v1", 1000, True, True),
        VotingStake(1, "v2", 64000, False, True),
        VotingStake(2, "v3", 500, True, False),
    )
    assert (
        evaluate_winner_payout(
            receipt=receipt(), stake=stake, backings=(backing("v1"),)
        ).agent_id
        == A
    )


def event(
    name: str,
    values: list[Any] | tuple[Any, ...] | dict[str, Any],
    phase: str = "Initialization",
) -> dict:
    return {
        "module_id": "SubtensorModule",
        "event_id": name,
        "phase": phase,
        "event": {"attributes": values},
    }


@pytest.fixture(params=[list, tuple], ids=["json-list", "scale-tuple"])
def chain(request: pytest.FixtureRequest) -> tuple[AsyncMock, dict]:
    substrate = AsyncMock()
    state: dict[str, Any] = {
        "runtime": sorted(AUDITED_RUNTIME_CODE_HASHES)[0],
        "events": [
            event("WeightsSet", request.param([118, 0])),
            event("TimelockedWeightsRevealed", request.param([118, "v1"])),
        ],
        "pending": [(9, [("v1", 80, "0x010203", 100)])],
        "keys": [(0, "v1"), (1, "miner"), (2, "tail")],
        "weights": [(0, [(1, 65535), (2, 35288)])],
        "step": 80,
    }
    substrate.get_chain_finalised_head.return_value = "h120"
    substrate.get_block_header.return_value = {"header": {"number": 120}}
    substrate.get_block_hash.side_effect = lambda n: f"h{n}"

    async def rpc(method: str, params: list) -> dict:
        assert method == "state_getStorageHash"
        return {
            "result": state.get("post_runtime", state["runtime"])
            if params[-1] == "h100"
            else state["runtime"]
        }

    substrate.rpc_request.side_effect = rpc

    async def query(**kw: Any) -> Any:
        return {
            "MechanismCountCurrent": 1,
            "CommitRevealWeightsEnabled": True,
            "Events": state["events"],
            "LastMechansimStepBlock": state["step"],
            "StakeWeight": [65535, 0, 0],
            "Active": [True, True, True],
            "ValidatorPermit": [True, False, False],
            "SubnetOwnerHotkey": "owner",
        }[kw["storage_function"]]

    substrate.query.side_effect = query

    async def mapping(**kw: Any) -> Any:
        kind = kw["storage_function"]
        data = {
            "Keys": state["keys"],
            "Weights": state["weights"],
            "TimelockedWeightCommits": state["pending"],
        }[kind]
        if kind == "Keys" and kw["block_hash"] == "h100":
            data = state.get("post_keys", data)

        async def rows() -> Any:
            for pair in data:
                yield pair

        return rows()

    substrate.query_map.side_effect = mapping
    return substrate, state


async def test_successful_singleton_reveal_binds_exact_ciphertext(chain: tuple) -> None:
    substrate, _ = chain
    block = await read_source_emission_block(substrate, netuid=118, block=100)
    update = block.updates[0]
    assert update.commit_block == 80 and update.reveal_round == 100
    assert update.commit_ciphertext_hash is not None
    assert update.vector_digest == vector_digest([(1, 65535), (2, 35288)])
    assert block.runtime_code_hash in AUDITED_RUNTIME_CODE_HASHES


@pytest.mark.parametrize(
    "case", ["two_pending", "two_successes", "extrinsic_write", "unknown_write"]
)
async def test_ambiguous_or_unknown_write_invalidates_even_equal_vector(
    chain: tuple, case: str
) -> None:
    substrate, state = chain
    if case == "two_pending":
        state["pending"][0][1].append(("v1", 81, "0x040506", 101))
    if case == "two_successes":
        state["events"] *= 2
    if case == "extrinsic_write":
        state["events"].append(event("WeightsSet", [118, 0], "ApplyExtrinsic"))
    if case == "unknown_write":
        state["events"] = [event("WeightsSet", [118, 0], "ApplyExtrinsic")]
    block = await read_source_emission_block(substrate, netuid=118, block=100)
    assert len(block.updates) == 1
    assert block.updates[0].commit_ciphertext_hash is None


async def test_removed_failed_commit_without_success_never_binds(chain: tuple) -> None:
    substrate, state = chain
    state["pending"] = []
    state["events"] = []
    block = await read_source_emission_block(substrate, netuid=118, block=100)
    assert block.updates == ()


async def test_payout_boundary_exposes_updates_for_collector_to_reject(
    chain: tuple,
) -> None:
    substrate, state = chain
    state["step"] = 100
    block = await read_source_emission_block(substrate, netuid=118, block=100)
    assert block.is_payout and block.updates
    assert block.voting_stake[0].stake_u16 == 65535
    assert dict(block.vector_digests)["v1"] == block.updates[0].vector_digest


async def test_uid_reuse_resets_all_provenance(chain: tuple) -> None:
    substrate, state = chain
    state["post_keys"] = [(0, "v1"), (1, "new-miner"), (2, "tail")]
    block = await read_source_emission_block(substrate, netuid=118, block=100)
    assert block.reset_reason == "uid_mapping_changed"
    assert set(block.invalidated_hotkeys) == {"miner", "new-miner", "v1"}
    assert block.updates == () and not block.is_payout


async def test_unknown_runtime_cannot_be_configured_around(chain: tuple) -> None:
    substrate, state = chain
    state["runtime"] = "0xunknown"
    with pytest.raises(ValueError, match="not audited"):
        await read_source_emission_block(substrate, netuid=118, block=100)
    with pytest.raises(ValueError, match="not audited"):
        await read_source_emission_block(
            substrate, netuid=118, block=100, expected_runtime_code_hash="0xunknown"
        )


@pytest.mark.parametrize("upgrade", [False, True])
async def test_v466_artifact_preserves_reveal_and_resets_upgrade_boundary(
    chain: tuple, upgrade: bool
) -> None:
    substrate, state = chain
    runtime = "0xff4ba0da10fb8ac26fab3e446f23413ef7f91de4a604802097ece0b928d53a8e"
    state["runtime"] = (
        "0x637844a3ad94d3bdbea45664b67bbfa07a31f21c087834a56a772ba27f612b9f"
        if upgrade
        else runtime
    )
    state["post_runtime"] = runtime
    block = await read_source_emission_block(substrate, netuid=118, block=100)
    assert block.runtime_code_hash == runtime
    if upgrade:
        assert block.reset_reason == "runtime_changed"
        assert not block.updates and not block.is_payout
    else:
        assert block.reset_reason is None
        assert block.updates[0].commit_ciphertext_hash is not None


async def test_known_runtime_transition_resets_provenance(chain: tuple) -> None:
    substrate, state = chain
    state["post_runtime"] = sorted(AUDITED_RUNTIME_CODE_HASHES)[1]
    block = await read_source_emission_block(substrate, netuid=118, block=100)
    assert block.reset_reason == "runtime_changed"


@pytest.mark.parametrize("upgrade", [False, True])
async def test_v467_artifact_preserves_reveal_and_resets_upgrade_boundary(
    chain: tuple, upgrade: bool
) -> None:
    substrate, state = chain
    v466 = "0xff4ba0da10fb8ac26fab3e446f23413ef7f91de4a604802097ece0b928d53a8e"
    v467 = "0x2f175dcc64196ec8a6b9235f8d7cfd84efef6c68bb925c4455949591cef9f6d2"
    state["runtime"] = v466 if upgrade else v467
    state["post_runtime"] = v467
    block = await read_source_emission_block(substrate, netuid=118, block=100)
    assert block.runtime_code_hash == v467
    if upgrade:
        assert block.reset_reason == "runtime_changed"
        assert not block.updates and not block.is_payout
    else:
        assert block.reset_reason is None
        assert block.updates[0].commit_ciphertext_hash is not None


@pytest.fixture
def commit_chain(chain: tuple) -> tuple:
    import hashlib
    from types import SimpleNamespace

    substrate, state = chain
    attribute_type = type(state["events"][0]["event"]["attributes"])
    raw = "0x123456"
    ciphertext = "0x010203"
    digest = hashlib.blake2b(bytes.fromhex(ciphertext[2:]), digest_size=32).hexdigest()
    attempt = SimpleNamespace(
        commit_block=100,
        commit_block_hash="h100",
        extrinsic_index=0,
        extrinsic_hash="0x"
        + hashlib.blake2b(bytes.fromhex(raw[2:]), digest_size=32).hexdigest(),
        ciphertext_hash=digest,
        reveal_round=100,
    )
    claim = SimpleNamespace(
        attempt=attempt, validator_hotkey="v1", netuid=118, mechanism_id=0
    )
    state["events"] = [
        event("ExtrinsicSuccess", [], "ApplyExtrinsic"),
        event(
            "TimelockedWeightsCommitted",
            attribute_type(["v1", 118, "0x" + digest, 100]),
            "ApplyExtrinsic",
        ),
    ]
    state["events"][0]["module_id"] = "System"
    for row in state["events"]:
        row["extrinsic_idx"] = 0
    state["extrinsic"] = {
        "address": "v1",
        "call": {
            "call_module": "SubtensorModule",
            "call_function": "commit_timelocked_mechanism_weights",
            "call_args": [
                {"name": k, "value": v}
                for k, v in {
                    "netuid": 118,
                    "mecid": 0,
                    "commit": ciphertext,
                    "reveal_round": 100,
                    "commit_reveal_version": 4,
                }.items()
            ],
        },
    }

    async def rpc(method: str, _params: list) -> dict:
        if method == "chain_getBlock":
            return {"result": {"block": {"extrinsics": [raw]}}}
        return {"result": state["runtime"]}

    substrate.rpc_request.side_effect = rpc

    async def decoded(**_kwargs: Any) -> dict:
        return {"extrinsics": [state["extrinsic"]]}

    substrate.get_block.side_effect = decoded
    return substrate, state, claim


async def test_exact_finalized_commit_inclusion(commit_chain: tuple) -> None:
    from ditto.chain.source_emission_verifier import verify_finalized_weight_commit

    substrate, _, claim = commit_chain
    await verify_finalized_weight_commit(substrate, claim)


@pytest.mark.parametrize("values", [(118,), (118, 0, 1), {"netuid": 118}, "118,0"])
async def test_malformed_event_attributes_still_fail_closed(
    chain: tuple, values: Any
) -> None:
    substrate, state = chain
    state["events"][0]["event"]["attributes"] = values
    with pytest.raises(ValueError, match="unsupported event schema"):
        await read_source_emission_block(substrate, netuid=118, block=100)


@pytest.mark.parametrize(
    "failure", ["failed", "hash", "signer", "ciphertext", "round", "event", "block"]
)
async def test_finalized_commit_claim_cannot_replace_chain_proof(
    commit_chain: tuple, failure: str
) -> None:
    from ditto.chain.source_emission_verifier import verify_finalized_weight_commit

    substrate, state, claim = commit_chain
    if failure == "failed":
        state["events"][0]["event_id"] = "ExtrinsicFailed"
    if failure == "hash":
        claim.attempt.extrinsic_hash = "0xwrong"
    if failure == "signer":
        state["extrinsic"]["address"] = "other"
    if failure == "ciphertext":
        claim.attempt.ciphertext_hash = "aa" * 32
    if failure == "round":
        claim.attempt.reveal_round = 101
    if failure == "event":
        state["events"].pop()
    if failure == "block":
        claim.attempt.commit_block_hash = "hwrong"
    with pytest.raises(ValueError):
        await verify_finalized_weight_commit(substrate, claim)


async def test_quiet_block_skips_heavy_weights_and_pending_maps(chain: tuple) -> None:
    substrate, state = chain
    state["events"] = []
    block = await read_source_emission_block(substrate, netuid=118, block=100)
    assert not block.is_payout and not block.updates
    assert [
        c.kwargs["storage_function"] for c in substrate.query_map.await_args_list
    ] == ["Keys", "Keys"]
    # Parent/current identity maps are necessary to detect even equal-vector UID reuse.
    assert substrate.query.await_count == 6


async def test_unrelated_uid_reuse_preserves_unaffected_validator(chain: tuple) -> None:
    substrate, state = chain
    state["keys"].append((3, "unrelated"))
    state["post_keys"] = [(0, "v1"), (1, "miner"), (2, "tail"), (3, "replacement")]
    state["events"] = []
    block = await read_source_emission_block(substrate, netuid=118, block=100)
    assert block.reset_reason == "uid_mapping_changed"
    assert set(block.invalidated_hotkeys) == {"unrelated", "replacement"}


@pytest.mark.parametrize(
    "missing", ["hash", "runtime", "raw", "decoded", "events", "behind"]
)
async def test_unavailable_historical_commit_proof_retries_archive(
    commit_chain: tuple, missing: str
) -> None:
    from ditto.chain.errors import ChainConnectionError
    from ditto.chain.source_emission_verifier import verify_finalized_weight_commit

    substrate, state, claim = commit_chain
    if missing == "hash":
        substrate.get_block_hash.side_effect = lambda _block: None
    elif missing == "runtime":
        state["runtime"] = None
    elif missing == "raw":
        original = substrate.rpc_request.side_effect

        async def rpc(method: str, params: list) -> dict:
            if method == "chain_getBlock":
                return {"result": None}
            return await original(method, params)

        substrate.rpc_request.side_effect = rpc
    elif missing == "decoded":
        substrate.get_block.side_effect = None
        substrate.get_block.return_value = None
    elif missing == "events":
        state["events"] = None
    elif missing == "behind":
        substrate.get_block_header.return_value = {"header": {"number": 99}}
    with pytest.raises(ChainConnectionError):
        await verify_finalized_weight_commit(substrate, claim)


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "late_reveal",
        "late_write",
        "extrinsic",
        "duplicate_payout",
        "ambiguous_commit",
    ],
)
@pytest.mark.parametrize(
    "runtime",
    [
        "0xff4ba0da10fb8ac26fab3e446f23413ef7f91de4a604802097ece0b928d53a8e",
        "0x2f175dcc64196ec8a6b9235f8d7cfd84efef6c68bb925c4455949591cef9f6d2",
    ],
    ids=["v466", "v467"],
)
async def test_payout_accepts_only_proven_initialization_order(
    chain: tuple, failure: str | None, runtime: str
) -> None:
    substrate, state = chain
    state["runtime"] = runtime
    state["step"] = 100
    payout = event("IncentiveAlphaEmittedToMiners", {"netuid": 118, "emissions": []})
    state["events"].append(payout)
    if failure == "late_reveal":
        state["events"][-2:] = state["events"][-2:][::-1]
    elif failure == "late_write":
        state["events"] = [payout] + state["events"][:-1]
    elif failure == "extrinsic":
        state["events"][0]["phase"] = "ApplyExtrinsic"
    elif failure == "duplicate_payout":
        state["events"].append(payout)
    elif failure == "ambiguous_commit":
        state["pending"][0][1].append(("v1", 81, "0x040506", 101))
    block = await read_source_emission_block(substrate, netuid=118, block=100)
    assert block.payout_initialization_reveals is (failure is None)
