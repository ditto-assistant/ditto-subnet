"""Synthetic enforcing fold controls, without a wallet or live weight setter."""

import math

import pytest
from bittensor_wallet import Keypair

from ditto.validator.treasury_weights import fold_treasury_weights
from ditto_screening_protocol.treasury import TreasuryEmissionPolicy
from ditto_screening_protocol.treasury_approval import (
    TreasuryPolicyApproval,
    approval_message,
)
from ditto_screening_protocol.treasury_enforcement import (
    EnforcingTreasuryPin,
    TreasuryFleetMember,
)


def fixture(*, service_bps=1000):
    coldkey = Keypair.create_from_uri("//Alice")
    policy = TreasuryEmissionPolicy(
        revision=1,
        genesis_hash="0x" + "11" * 32,
        collector_hotkey=Keypair.create_from_uri("//Bob").ss58_address,
        collector_coldkey=coldkey.ss58_address,
        collector_policy_digest="c" * 64,
        buckets=[
            {
                "bucket_id": "gamma",
                "allocation_bps": service_bps,
                "holding_coldkey": Keypair.create_from_uri("//Charlie").ss58_address,
            }
        ],
    )
    approval = TreasuryPolicyApproval(
        policy=policy,
        signature="0x" + coldkey.sign(approval_message(policy)).hex(),
    )
    member = TreasuryFleetMember(
        validator_hotkey=Keypair.create_from_uri("//Eve").ss58_address,
        protocol_version=30,
        treasury_pin_version=2,
        treasury_dispatch_version=2,
        approved_policy_digest=policy.digest,
        collector_policy_digest="c" * 64,
    )
    pin = EnforcingTreasuryPin(
        epoch_index=9,
        first_block=100,
        pinned_block=101,
        pinned_block_hash="0x" + "ab" * 32,
        policy=policy,
        policy_digest=policy.digest,
        approval=approval,
        fleet=[member],
        identity={
            "genesis_hash": policy.genesis_hash,
            "finalized_block": 101,
            "finalized_block_hash": "0x" + "ab" * 32,
            "uid": 0,
            "hotkey": policy.collector_hotkey,
            "uid_hotkey": policy.collector_hotkey,
            "owner_coldkey": policy.collector_coldkey,
            "subnet_owner_coldkey": Keypair.create_from_uri("//Dave").ss58_address,
        },
    )
    current = pin.identity.model_copy(
        update={
            "finalized_block": 102,
            "finalized_block_hash": "0x" + "ac" * 32,
        }
    )
    return pin, {
        "pin": pin,
        "burn_share": 0.0,
        "paid_miner_fraction": 1.0,
        "burn_hotkey": Keypair.create_from_uri("//Dave").ss58_address,
        "expected_policy_digest": policy.digest,
        "expected_collector_policy_digest": policy.collector_policy_digest,
        "local_capability": member,
        "current_identity": current,
        "netuid": 118,
        "current_epoch_index": 9,
        "current_first_block": 100,
        "finalized_block": 102,
        "finalized_block_hash": "0x" + "ac" * 32,
    }


@pytest.mark.parametrize("burn", [0, 0.2, 0.5, 1])
@pytest.mark.parametrize("paid", [0, 0.3, 1])
def test_service_reserved_before_burn_and_unpaid_remainder(burn, paid):
    pin, args = fixture()
    result = fold_treasury_weights(
        {"miner": 1}, **(args | {"burn_share": burn, "paid_miner_fraction": paid})
    )
    assert result[pin.policy.collector_hotkey] == 0.1
    assert result.get("miner", 0) == pytest.approx(0.9 * (1 - burn) * paid)
    assert math.fsum(result.values()) == pytest.approx(1)
    if burn == 1:
        assert result[args["burn_hotkey"]] == 0.9


@pytest.mark.parametrize("service_bps", [0, 1000])
def test_collector_is_excluded_from_competition_even_with_zero_pool(service_bps):
    pin, args = fixture(service_bps=service_bps)
    result = fold_treasury_weights(
        {"miner": 1, pin.policy.collector_hotkey: 10**100}, **args
    )
    assert result.get(pin.policy.collector_hotkey, 0) == service_bps / 10_000
    assert result["miner"] == pytest.approx(1 - service_bps / 10_000)


def test_empty_miner_vector_does_not_enlarge_service_pool():
    pin, args = fixture()
    assert fold_treasury_weights({}, **args) == {
        pin.policy.collector_hotkey: 0.1,
        args["burn_hotkey"]: 0.9,
    }


@pytest.mark.parametrize(
    "fault",
    [
        "missing_crypto",
        "wrong_signer",
        "wrong_deployment",
        "changed_collector_digest",
        "changed_uid",
        "uid_reused",
        "changed_owner",
        "owner_associated",
        "wrong_chain",
        "wrong_netuid",
        "stale_identity",
        "hash_mismatch",
        "wrong_epoch",
        "wrong_first_block",
        "legacy_dispatch",
        "legacy_protocol",
        "absent_self",
        "empty_fleet",
        "mixed_fleet",
        "burn_collector",
        "bool_height",
        "shadow_mode",
    ],
)
def test_refusal_preserves_caller_vector(fault):
    pin, args = fixture()
    if fault in {"missing_crypto", "wrong_signer"}:
        signature = (
            bytes(64)
            if fault == "missing_crypto"
            else Keypair.create_from_uri("//Dave").sign(approval_message(pin.policy))
        )
        args["pin"] = pin.model_copy(
            update={
                "approval": pin.approval.model_copy(
                    update={"signature": "0x" + signature.hex()}
                )
            }
        )
    elif fault == "wrong_deployment":
        args["expected_policy_digest"] = "e" * 64
    elif fault == "changed_collector_digest":
        args["expected_collector_policy_digest"] = "e" * 64
    elif fault == "changed_uid":
        args["current_identity"] = args["current_identity"].model_copy(
            update={"uid": 1}
        )
    elif fault == "uid_reused":
        args["current_identity"] = args["current_identity"].model_copy(
            update={"uid_hotkey": args["burn_hotkey"]}
        )
    elif fault in {"changed_owner", "owner_associated"}:
        changes = (
            {"owner_coldkey": Keypair.create_from_uri("//Ferdie").ss58_address}
            if fault == "changed_owner"
            else {"subnet_owner_coldkey": pin.policy.collector_coldkey}
        )
        args["current_identity"] = args["current_identity"].model_copy(update=changes)
    elif fault == "wrong_chain":
        args["current_identity"] = args["current_identity"].model_copy(
            update={"genesis_hash": "0x" + "22" * 32}
        )
    elif fault == "wrong_netuid":
        args["netuid"] = 119
    elif fault == "stale_identity":
        args["finalized_block"] = 103
    elif fault == "hash_mismatch":
        args["finalized_block_hash"] = "0x" + "ff" * 32
    elif fault == "wrong_epoch":
        args["current_epoch_index"] = 10
    elif fault == "wrong_first_block":
        args["current_first_block"] = 99
    elif fault in {"legacy_dispatch", "legacy_protocol"}:
        key, value = (
            ("treasury_dispatch_version", 1)
            if fault == "legacy_dispatch"
            else ("protocol_version", 29)
        )
        args["local_capability"] = args["local_capability"].model_copy(
            update={key: value}
        )
    elif fault == "absent_self":
        args["local_capability"] = args["local_capability"].model_copy(
            update={"validator_hotkey": args["burn_hotkey"]}
        )
    elif fault == "empty_fleet":
        args["pin"] = pin.model_copy(update={"fleet": ()})
    elif fault == "mixed_fleet":
        old = args["local_capability"].model_copy(update={"protocol_version": 29})
        args["pin"] = pin.model_copy(update={"fleet": (old,)})
    elif fault == "burn_collector":
        args["burn_hotkey"] = pin.policy.collector_hotkey
    elif fault == "bool_height":
        args["finalized_block"] = True
    else:
        args["pin"] = pin.model_copy(update={"mode": "shadow"})
    vector = {"miner": 1}
    with pytest.raises(ValueError):
        fold_treasury_weights(vector, **args)
    assert vector == {"miner": 1}
