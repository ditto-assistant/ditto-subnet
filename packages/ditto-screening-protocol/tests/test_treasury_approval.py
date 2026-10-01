import json
from pathlib import Path

import pytest

from ditto_screening_protocol.treasury import TreasuryLedgerPin
from ditto_screening_protocol.treasury_approval import (
    TreasuryPolicyApproval,
    approval_message,
    verify_pinned_policy_approval,
    verify_policy_approval,
)


def pin():
    return TreasuryLedgerPin.model_validate_json(
        (Path(__file__).parent / "fixtures/treasury_ledger_pin_v1.json").read_text()
    )


def approval():
    return TreasuryPolicyApproval(policy=pin().policy, signature="0x" + "ab" * 64)


def verify(value, verifier=lambda *_: True, **changes):
    args = {
        "expected_policy_digest": pin().policy.digest,
        "expected_collector_policy_digest": pin().policy.collector_policy_digest,
        "verify_signature": verifier,
    }
    return verify_policy_approval(value, **{**args, **changes})


def test_verifier_receives_only_pinned_public_coldkey_and_separate_action_domain():
    calls = []

    def verifier(*args):
        calls.append(args)
        return True

    assert verify(approval(), verifier) == pin().policy
    assert calls == [
        (pin().policy.collector_coldkey, approval_message(pin().policy), b"\xab" * 64)
    ]
    assert approval_message(pin().policy) == (
        f"ditto-treasury-emission-policy-v1:{pin().policy.digest}".encode()
    )


@pytest.mark.parametrize("result", [False, 1, "verified", None])
def test_verification_requires_literal_cryptographic_true(result):
    with pytest.raises(ValueError):
        verify(approval(), lambda *_: result)


@pytest.mark.parametrize(
    "change",
    [
        {"expected_policy_digest": "a" * 64},
        {"expected_collector_policy_digest": "a" * 64},
        {"expected_policy_digest": "bad"},
    ],
)
def test_deployment_digest_mismatch_refuses_before_crypto(change):
    def never(*_):
        raise AssertionError("mismatched deployment pin reached crypto")

    with pytest.raises(ValueError):
        verify(approval(), never, **change)


def test_unknown_fields_do_not_change_signed_known_policy():
    raw = approval().model_dump(mode="json")
    raw["verified"] = True
    raw["policy"]["future"] = True
    raw["policy"]["buckets"][0]["future"] = True
    validated = TreasuryPolicyApproval.model_validate_json(json.dumps(raw))
    assert verify(validated) == pin().policy
    assert "verified" not in validated.model_dump()


def test_preconstructed_model_cannot_skip_policy_revalidation():
    altered = approval().model_copy(
        update={"policy": pin().policy.model_copy(update={"netuid": 119})}
    )
    with pytest.raises(ValueError):
        verify(altered)


@pytest.mark.parametrize(
    "change",
    [
        {"netuid": 119},
        {"first_block": 102},
        {"pinned_block": 100},
        {"pinned_block_hash": "0x" + "ef" * 32},
        {"pinned_block": True},
    ],
)
def test_approved_policy_does_not_authorize_wrong_or_stale_epoch(change):
    args = {
        "expected_policy_digest": pin().policy.digest,
        "expected_collector_policy_digest": pin().policy.collector_policy_digest,
        "verify_signature": lambda *_: True,
        "netuid": 118,
        "first_block": 100,
        "pinned_block": 101,
        "pinned_block_hash": pin().identity.finalized_block_hash,
    }
    with pytest.raises(ValueError):
        verify_pinned_policy_approval(pin(), approval(), **{**args, **change})
