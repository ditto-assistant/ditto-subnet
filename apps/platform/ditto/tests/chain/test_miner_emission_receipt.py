"""Raw storage receipt tests: weight intent must not substitute for payout."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ditto.chain import ChainClient, ChainConfig, ChainConnectionError
from ditto.chain.errors import ChainEmissionReceiptUnavailable


@pytest.fixture
def receipt_chain(install_substrate_module: AsyncMock) -> dict[str, Any]:
    substrate = install_substrate_module
    substrate.get_chain_finalised_head.return_value = "h1200"
    substrate.get_block_header.return_value = {"header": {"number": 1200}}
    substrate.get_block_hash.side_effect = lambda block: f"h{block}"
    state: dict[str, Any] = {
        "events": [
            {
                "module_id": "SubtensorModule",
                "event_id": "IncentiveAlphaEmittedToMiners",
                "phase": "Initialization",
                "event": {
                    "attributes": {"netuid": 118, "emissions": [900, 200, 0, 100]}
                },
            }
        ],
        "keys": [(0, "burn"), (1, "miner"), (2, "validator"), (3, "owner-associated")],
        "weights": [(2, [(0, 32768), (1, 32767)])],
        "updates": [0, 0, 950, 0],
        "mechanisms": 1,
        "step": 1000,
        "timestamp": 1000000,
    }

    async def query(**kwargs: Any) -> Any:
        name, at = kwargs["storage_function"], kwargs["block_hash"]
        values = {
            "LastMechansimStepBlock": 640 if at == "h999" else state["step"],
            "MechanismCountCurrent": state["mechanisms"],
            "SubnetEpochIndex": 3,
            "SubnetOwner": "owner",
            "SubnetOwnerHotkey": "burn",
            "OwnedHotkeys": ["owner-associated"],
            "Events": state["events"],
            "LastUpdate": state.get("post_updates", state["updates"])
            if at == "h1000"
            else state["updates"],
            "Now": state["timestamp"] if at == "h1000" else 950000,
        }
        if at == "h1000" and name in state.get("post_ownership", {}):
            return state["post_ownership"][name]
        return values[name]

    async def query_map(**kwargs: Any) -> Any:
        rows = (
            state["keys"] if kwargs["storage_function"] == "Keys" else state["weights"]
        )
        if kwargs["storage_function"] == "Weights" and kwargs["block_hash"] == "h1000":
            rows = state.get("post_weights", rows)

        async def items() -> Any:
            for row in rows:
                yield row

        return items()

    substrate.query.side_effect = query
    substrate.query_map.side_effect = query_map
    return state


def client() -> ChainClient:
    return ChainClient(
        ChainConfig(pylon_url="http://unused", netuid=118, open_access_token="fake")
    )


@pytest.mark.usefixtures("receipt_chain")
async def test_miner_incentive_excludes_owner_burn(
    install_substrate_module: AsyncMock,
) -> None:
    result = await client().get_miner_emission_receipt(118)
    assert [(e.hotkey, e.amount_rao) for e in result.earnings] == [("miner", 200)]
    assert result.block == 1000 and result.previous_distribution_block == 640
    assert result.block_timestamp == 1000 and result.epoch_index == 3
    assert result.validator_last_updates == ((2, 950),)
    assert result.validator_last_update_timestamps == ((2, 950),)
    assert result.vectors[0].weights[1].hotkey == "miner"
    install_substrate_module.get_chain_head.assert_not_awaited()
    key_reads = [
        c.kwargs
        for c in install_substrate_module.query_map.await_args_list
        if c.kwargs["storage_function"] == "Keys"
    ]
    assert len(key_reads) == 1 and key_reads[0]["block_hash"] == "h999"


@pytest.mark.parametrize(
    "failure",
    [
        "missing_event",
        "duplicate_event",
        "wrong_phase",
        "unknown_schema",
        "multimechanism",
        "nonfinalized",
        "missing_keys",
        "bad_amount",
        "changed_weights",
        "changed_updates",
        "missing_timestamp",
        "future_update",
        "duplicate_key",
        "wrong_netuid",
    ],
)
async def test_missing_payout_fails_closed(
    receipt_chain: dict[str, Any], failure: str
) -> None:
    state = receipt_chain
    if failure == "missing_event":
        state["events"] = []
    elif failure == "duplicate_event":
        state["events"] *= 2
    elif failure == "wrong_phase":
        state["events"][0]["phase"] = "ApplyExtrinsic"
    elif failure == "unknown_schema":
        state["events"][0]["event"]["attributes"] = [118, [900, 200, 0, 100]]
    elif failure == "multimechanism":
        state["mechanisms"] = 2
    elif failure == "nonfinalized":
        state["step"] = 1201
    elif failure == "missing_keys":
        state["keys"] = state["keys"][:-1]
    elif failure == "bad_amount":
        state["events"][0]["event"]["attributes"]["emissions"][1] = True
    elif failure == "changed_weights":
        state["post_weights"] = [(2, [(1, 65535)])]
    elif failure == "changed_updates":
        state["post_updates"] = [0, 0, 1000, 0]
    elif failure == "missing_timestamp":
        state["timestamp"] = None
    elif failure == "future_update":
        state["updates"][2] = 1001
    elif failure == "duplicate_key":
        state["keys"].append((1, "different"))
    elif failure == "wrong_netuid":
        state["events"][0]["event"]["attributes"]["netuid"] = 119
    with pytest.raises(
        ChainEmissionReceiptUnavailable
        if failure in {"changed_weights", "changed_updates"}
        else ChainConnectionError
    ):
        await client().get_miner_emission_receipt(118)


async def test_zero_earning_despite_positive_weights(
    receipt_chain: dict[str, Any],
) -> None:
    receipt_chain["events"][0]["event"]["attributes"]["emissions"][1] = 0
    result = await client().get_miner_emission_receipt(118)
    assert result.earnings == ()
    assert result.vectors[0].weights[1].value > 0


@pytest.mark.usefixtures("receipt_chain")
async def test_irrelevant_vector_needs_no_historical_timestamp(
    install_substrate_module: AsyncMock,
) -> None:
    result = await client().get_miner_emission_receipt(
        118, target_hotkeys=frozenset({"different-miner"})
    )
    assert result.vectors and result.validator_last_updates == ((2, 950),)
    assert result.validator_last_update_timestamps == ()
    assert all(
        c.args != (950,)
        for c in install_substrate_module.get_block_hash.await_args_list
    )


@pytest.mark.usefixtures("receipt_chain")
async def test_old_relevant_timestamp_uses_archive_fallback(
    install_substrate_module: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = install_substrate_module.query.side_effect

    async def pruned(**kwargs: Any) -> Any:
        if kwargs["storage_function"] == "Now" and kwargs["block_hash"] == "h950":
            raise ValueError("State already discarded")
        return await original(**kwargs)

    install_substrate_module.query.side_effect = pruned
    archive = AsyncMock(return_value=950)
    monkeypatch.setattr(ChainClient, "get_block_timestamp", archive)
    result = await client().get_miner_emission_receipt(
        118, target_hotkeys=frozenset({"miner"})
    )
    assert result.validator_last_update_timestamps == ((2, 950),)
    archive.assert_awaited_once_with("h950")


def test_installed_scale_decoder_preserves_named_event_attributes() -> None:
    # Exercise the installed cyscale decoder used by substrate-interface, not
    # an invented fallback shape. The runtime event uses named Rust fields.
    from scalecodec import ScaleBytes
    from scalecodec.base import RuntimeConfigurationObject

    runtime = RuntimeConfigurationObject()
    runtime.update_type_registry(
        {
            "types": {
                "ReceiptAttributes": {
                    "type": "struct",
                    "type_mapping": [["netuid", "u16"], ["emissions", "Vec<u64>"]],
                },
                "ReceiptPalletEvent": {
                    "type": "enum",
                    "type_mapping": [
                        ["IncentiveAlphaEmittedToMiners", "ReceiptAttributes"]
                    ],
                },
                "ReceiptEvent": {
                    "type": "enum",
                    "base_class": "GenericScaleInfoEvent",
                    "type_mapping": [["SubtensorModule", "ReceiptPalletEvent"]],
                },
            }
        }
    )
    decoded = runtime.create_scale_object(
        "ReceiptEvent", data=ScaleBytes("0x00007600080000000000000000c800000000000000")
    ).decode()
    assert decoded["attributes"] == {"netuid": 118, "emissions": [0, 200]}
    assert decoded["module_id"] == "SubtensorModule"
    assert decoded["event_id"] == "IncentiveAlphaEmittedToMiners"


@pytest.mark.parametrize(
    "storage,value",
    [
        ("SubnetOwner", "new-owner"),
        ("SubnetOwnerHotkey", "miner"),
        ("OwnedHotkeys", ["owner-associated", "miner"]),
    ],
)
async def test_ownership_transition_cannot_turn_burn_into_earnings(
    receipt_chain: dict[str, Any], storage: str, value: Any
) -> None:
    # The event still reports positive gross miner incentive. A new owner
    # association before payout can recycle that incentive instead of paying it.
    receipt_chain["post_ownership"] = {storage: value}
    with pytest.raises(ChainEmissionReceiptUnavailable, match="ownership changed"):
        await client().get_miner_emission_receipt(118)


@pytest.mark.usefixtures("receipt_chain")
async def test_historical_payout_does_not_follow_latest_step(
    install_substrate_module: AsyncMock,
) -> None:
    original = install_substrate_module.query.side_effect

    async def historical(**kwargs: Any) -> Any:
        if (
            kwargs["storage_function"] == "LastMechansimStepBlock"
            and kwargs["block_hash"] == "h1200"
        ):
            return 1100
        return await original(**kwargs)

    install_substrate_module.query.side_effect = historical
    result = await client().get_miner_emission_receipt(118, payout_block=1000)
    assert result.block == 1000
    with pytest.raises(ChainConnectionError, match="finalized"):
        await client().get_miner_emission_receipt(118, payout_block=1201)


@pytest.mark.usefixtures("receipt_chain")
@pytest.mark.parametrize(
    "error", [ValueError("State already discarded"), OSError("RPC unavailable")]
)
async def test_historical_rpc_error_remains_retryable(
    install_substrate_module: AsyncMock, error: Exception
) -> None:
    install_substrate_module.query.side_effect = error
    with pytest.raises(ChainConnectionError):
        await client().get_miner_emission_receipt(118, payout_block=1000)


async def test_historical_commit_at_payout_is_terminal(
    receipt_chain: dict[str, Any],
) -> None:
    receipt_chain["post_updates"] = [0, 0, 1000, 0]
    with pytest.raises(ChainEmissionReceiptUnavailable):
        await client().get_miner_emission_receipt(118, payout_block=1000)


async def test_missing_post_update_array_remains_retryable(
    receipt_chain: dict[str, Any],
) -> None:
    receipt_chain["post_updates"] = None
    with pytest.raises(ChainConnectionError):
        await client().get_miner_emission_receipt(118, payout_block=1000)


@pytest.mark.usefixtures("receipt_chain")
async def test_historical_receipt_reuses_archive_transport(
    install_substrate_module: AsyncMock,
) -> None:
    result = await client().get_miner_emission_receipt(
        118,
        payout_block=1000,
        substrate=install_substrate_module,
    )
    assert result.block == 1000
    install_substrate_module.__aenter__.assert_not_awaited()
    install_substrate_module.__aexit__.assert_not_awaited()


async def test_missing_post_weight_matrix_remains_retryable(
    receipt_chain: dict[str, Any],
) -> None:
    receipt_chain["post_weights"] = []
    with pytest.raises(ChainConnectionError):
        await client().get_miner_emission_receipt(118, payout_block=1000)


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "late_write",
        "unexplained_weights",
        "unexplained_update",
        "unknown_runtime",
    ],
)
async def test_initialization_reveal_receipt_uses_proven_consumed_vector(
    receipt_chain, install_substrate_module, failure
):
    from ditto.chain.source_emission_verifier import AUDITED_RUNTIME_CODE_HASHES

    state, substrate = receipt_chain, install_substrate_module
    query, mapping = substrate.query.side_effect, substrate.query_map.side_effect
    state["post_weights"] = [(2, [(1, 65535)])]
    state["post_updates"] = [0, 0, 1000, 0]
    state["events"][:0] = [
        {
            "module_id": "SubtensorModule",
            "event_id": name,
            "phase": "Initialization",
            "event": {"attributes": attrs},
        }
        for name, attrs in [
            ("WeightsSet", (118, 2)),
            ("TimelockedWeightsRevealed", (118, "validator")),
        ]
    ]

    async def enriched_query(**kw):
        extras = {
            "CommitRevealWeightsEnabled": True,
            "StakeWeight": [0, 0, 65535, 0],
            "Active": [False, False, True, False],
            "ValidatorPermit": [False, False, True, False],
        }
        if kw["storage_function"] in extras:
            return extras[kw["storage_function"]]
        return await query(**kw)

    async def enriched_mapping(**kw):
        if kw["storage_function"] == "TimelockedWeightCommits":

            async def rows():
                yield 1, [("validator", 900, "0x010203", 123)]

            return rows()
        return await mapping(**kw)

    substrate.query.side_effect = enriched_query
    substrate.query_map.side_effect = enriched_mapping
    substrate.rpc_request.return_value = {
        "result": sorted(AUDITED_RUNTIME_CODE_HASHES)[0]
    }
    if failure == "late_write":
        state["events"].append(state["events"].pop(0))
    elif failure == "unexplained_weights":
        state["post_weights"].append((1, [(2, 1)]))
    elif failure == "unexplained_update":
        state["post_updates"][1] = 1000
    elif failure == "unknown_runtime":
        substrate.rpc_request.return_value = {"result": "0xunknown"}
    if failure:
        with pytest.raises((ChainEmissionReceiptUnavailable, ChainConnectionError)):
            await client().get_miner_emission_receipt(
                118, allow_initialization_reveals=True
            )
    else:
        result = await client().get_miner_emission_receipt(
            118, allow_initialization_reveals=True
        )
        assert result.validator_last_updates == ((2, 1000),)
        assert result.validator_last_update_timestamps == ((2, 1000),)
        assert [(w.hotkey, w.value) for w in result.vectors[0].weights] == [
            ("miner", 65535)
        ]
