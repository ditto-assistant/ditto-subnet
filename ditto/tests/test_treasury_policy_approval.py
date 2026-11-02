"""Real synthetic key signatures: no wallet, key file or chain is accessed."""

import pytest
from bittensor_wallet import Keypair

from ditto_screening_protocol.treasury import TreasuryEmissionPolicy, TreasuryLedgerPin
from ditto_screening_protocol.treasury_approval import (
    TreasuryPolicyApproval,
    approval_message,
    verify_pinned_policy_approval,
    verify_policy_approval,
    verify_public_signature,
)


def signed_proposal():
    # Public deterministic fixtures, never production credentials.
    coldkey = Keypair.create_from_uri("//Alice")
    hotkey = Keypair.create_from_uri("//Bob")
    holding = Keypair.create_from_uri("//Charlie")
    policy = TreasuryEmissionPolicy(
        revision=7,
        genesis_hash="0x" + "11" * 32,
        collector_hotkey=hotkey.ss58_address,
        collector_coldkey=coldkey.ss58_address,
        collector_policy_digest="c" * 64,
        buckets=[
            {
                "bucket_id": "gamma",
                "allocation_bps": 1000,
                "holding_coldkey": holding.ss58_address,
            }
        ],
    )
    return coldkey, TreasuryPolicyApproval(
        policy=policy, signature="0x" + coldkey.sign(approval_message(policy)).hex()
    )


def verify(approval, original):
    return verify_policy_approval(
        approval,
        expected_policy_digest=original.policy.digest,
        expected_collector_policy_digest=original.policy.collector_policy_digest,
        verify_signature=verify_public_signature,
    )


def test_exact_offline_coldkey_approval_verifies_with_real_crypto():
    _, approval = signed_proposal()
    assert verify(approval, approval) == approval.policy


@pytest.mark.parametrize("fault", ["wrong_signer", "other_action", "zero_signature"])
def test_wrong_signature_cannot_approve_emission(fault):
    coldkey, original = signed_proposal()
    if fault == "wrong_signer":
        signature = Keypair.create_from_uri("//Dave").sign(
            approval_message(original.policy)
        )
    elif fault == "other_action":
        signature = coldkey.sign(
            f"ditto-collector-policy-v1:{original.policy.digest}".encode()
        )
    else:
        signature = bytes(64)
    altered = original.model_copy(update={"signature": "0x" + signature.hex()})
    with pytest.raises(ValueError):
        verify(altered, original)


@pytest.mark.parametrize(
    "field",
    ["revision", "genesis_hash", "collector_policy_digest", "destination", "netuid"],
)
def test_changed_known_policy_cannot_reuse_signature(field):
    _, original = signed_proposal()
    raw = original.model_dump(mode="json")
    if field == "revision":
        raw["policy"][field] += 1
    elif field == "destination":
        raw["policy"]["buckets"][0]["holding_coldkey"] = Keypair.create_from_uri(
            "//Dave"
        ).ss58_address
    elif field == "netuid":
        raw["policy"][field] = 119
    elif field == "genesis_hash":
        raw["policy"][field] = "0x" + "22" * 32
    else:
        raw["policy"][field] = "d" * 64
    with pytest.raises(ValueError):
        verify(TreasuryPolicyApproval.model_validate(raw), original)
    if field != "netuid":
        altered = TreasuryPolicyApproval.model_validate(raw)
        # Repinning cannot make an old signature approve different known bytes.
        with pytest.raises(ValueError):
            verify(altered, altered)


@pytest.mark.parametrize("signature", [None, "", "0x11", "0x" + "zz" * 64])
def test_missing_or_malformed_signature_refuses(signature):
    _, original = signed_proposal()
    with pytest.raises(ValueError):
        TreasuryPolicyApproval.model_validate(
            {"policy": original.policy, "signature": signature}
        )


def test_invalid_ss58_checksum_cannot_be_reported_verified():
    assert (
        verify_public_signature("5" + "A" * 47, b"public fixture", bytes(64)) is False
    )


def epoch_pin(approval):
    policy = approval.policy
    return TreasuryLedgerPin(
        policy=policy,
        policy_digest=policy.digest,
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


def verify_epoch(pin, approval):
    return verify_pinned_policy_approval(
        pin,
        approval,
        expected_policy_digest=approval.policy.digest,
        expected_collector_policy_digest=approval.policy.collector_policy_digest,
        verify_signature=verify_public_signature,
        netuid=118,
        first_block=100,
        pinned_block=101,
        pinned_block_hash="0x" + "ab" * 32,
    )


def test_real_signature_binds_shadow_epoch_without_authorizing_dispatch():
    _, approval = signed_proposal()
    validated = verify_epoch(epoch_pin(approval), approval)
    assert validated.mode == "shadow"
    assert validated.identity.uid == 0


@pytest.mark.parametrize("fault", ["reused_uid", "owner", "genesis", "stale", "active"])
def test_signed_policy_cannot_launder_invalid_epoch_identity(fault):
    _, approval = signed_proposal()
    raw = epoch_pin(approval).model_dump(mode="json")
    if fault == "reused_uid":
        raw["identity"]["uid_hotkey"] = Keypair.create_from_uri("//Eve").ss58_address
    elif fault == "owner":
        raw["identity"]["subnet_owner_coldkey"] = approval.policy.collector_coldkey
    elif fault == "genesis":
        raw["identity"]["genesis_hash"] = "0x" + "22" * 32
    elif fault == "stale":
        raw["identity"]["finalized_block"] = 99
    else:
        raw["mode"] = "enforce"
    with pytest.raises(ValueError):
        verify_epoch(TreasuryLedgerPin.model_validate(raw), approval)
