"""Coldkey-signed Gamma ranges: the operator moves inside, never outside, them."""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ditto_screening_protocol.treasury import TreasuryEmissionPolicy
from ditto_screening_protocol.treasury_approval import TreasuryPolicyApproval
from ditto_screening_protocol.treasury_enforcement import (
    EnforcingTreasuryPin,
    TreasuryFleetMember,
)

# Public synthetic SS58 strings; syntax only, never custody.
COLLECTOR = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
COLDKEY = "5GrwvaEF5zXb26Fz9rcQpDWS5Rp1fu4uZ6V8SwrWGpvGNNsx"
HOLDING = "5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y"
OTHER = "5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy"
VALIDATOR = "5HGjWAeFDfFCWPsjFQdVV2Msvz2XtMktvgocEZcCj68kUMaw"


def policy(**bucket):
    return TreasuryEmissionPolicy(
        revision=3,
        genesis_hash="0x" + "11" * 32,
        collector_hotkey=COLLECTOR,
        collector_coldkey=COLDKEY,
        collector_policy_digest="c" * 64,
        buckets=[
            {"bucket_id": "gamma", "allocation_bps": 2500, "holding_coldkey": HOLDING}
            | bucket
        ],
    )


def ranged():
    return policy(min_allocation_bps=0, max_allocation_bps=7500)


def enforcing(signed, allocations=None):
    return EnforcingTreasuryPin(
        epoch_index=9,
        first_block=100,
        pinned_block=101,
        pinned_block_hash="0x" + "ab" * 32,
        policy=signed,
        policy_digest=signed.digest,
        approval=TreasuryPolicyApproval(policy=signed, signature="0x" + "ab" * 64),
        fleet=[
            TreasuryFleetMember(
                validator_hotkey=VALIDATOR,
                protocol_version=30,
                treasury_pin_version=2,
                treasury_dispatch_version=2,
                approved_policy_digest=signed.digest,
                collector_policy_digest="c" * 64,
            )
        ],
        identity={
            "genesis_hash": signed.genesis_hash,
            "finalized_block": 101,
            "finalized_block_hash": "0x" + "ab" * 32,
            "uid": 4,
            "hotkey": COLLECTOR,
            "uid_hotkey": COLLECTOR,
            "owner_coldkey": COLDKEY,
            "subnet_owner_coldkey": OTHER,
        },
        allocations=allocations,
    )


def test_fixed_policy_digest_is_byte_identical_to_the_pre_range_encoding():
    # Hash the stored pre-range JSON directly, independent of the model.
    raw = json.loads(
        (Path(__file__).parent / "fixtures/treasury_ledger_pin_v1.json").read_text()
    )["policy"]
    body = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    assert (
        TreasuryEmissionPolicy.model_validate(raw).digest
        == hashlib.sha256(
            ("ditto-treasury-emission-policy-v1:" + body).encode()
        ).hexdigest()
    )
    fixed = policy()
    assert not fixed.ranged
    assert set(fixed.model_dump()["buckets"][0]) == {
        "bucket_id",
        "allocation_bps",
        "holding_coldkey",
    }


def test_range_is_part_of_the_signed_digest():
    assert ranged().digest != policy().digest
    assert (
        ranged().digest != policy(min_allocation_bps=0, max_allocation_bps=8000).digest
    )
    assert ranged().model_dump()["buckets"][0]["max_allocation_bps"] == 7500


@pytest.mark.parametrize(
    "bucket",
    [
        {"min_allocation_bps": 0},
        {"max_allocation_bps": 7500},
        {"min_allocation_bps": 3000, "max_allocation_bps": 7500},
        {"min_allocation_bps": 0, "max_allocation_bps": 2000},
        {"min_allocation_bps": 0, "max_allocation_bps": 10001},
    ],
)
def test_malformed_signed_range_refuses(bucket):
    with pytest.raises(ValidationError):
        policy(**bucket)


def test_every_in_range_choice_must_fit_the_emission():
    with pytest.raises(ValidationError, match="exceeds 10000"):
        TreasuryEmissionPolicy(
            revision=3,
            genesis_hash="0x" + "11" * 32,
            collector_hotkey=COLLECTOR,
            collector_coldkey=COLDKEY,
            collector_policy_digest="c" * 64,
            buckets=[
                {
                    "bucket_id": "gamma",
                    "allocation_bps": 5000,
                    "holding_coldkey": HOLDING,
                    "min_allocation_bps": 0,
                    "max_allocation_bps": 6000,
                },
                {
                    "bucket_id": "bitsec",
                    "allocation_bps": 1000,
                    "holding_coldkey": OTHER,
                    "min_allocation_bps": 0,
                    "max_allocation_bps": 5000,
                },
            ],
        )


def test_admits_names_every_bucket_inside_its_range():
    signed = ranged()
    assert signed.admits({"gamma": 0})
    assert signed.admits({"gamma": 7500})
    assert not signed.admits({"gamma": 7501})
    assert not signed.admits({})
    assert not signed.admits({"gamma": 5000, "bitsec": 0})
    assert policy().admits({"gamma": 2500})
    assert not policy().admits({"gamma": 2400})


def test_enforcing_pin_folds_its_pinned_in_range_allocation():
    pin = enforcing(ranged(), [{"bucket_id": "gamma", "allocation_bps": 6000}])
    assert pin.service_bps == 6000
    assert pin.policy.service_bps == 2500
    (bucket,) = pin.effective_buckets
    assert (bucket.bucket_id, bucket.allocation_bps, bucket.holding_coldkey) == (
        "gamma",
        6000,
        HOLDING,
    )
    assert EnforcingTreasuryPin.model_validate(pin.model_dump(mode="json")) == pin


@pytest.mark.parametrize(
    "allocations",
    [
        None,
        [{"bucket_id": "gamma", "allocation_bps": 7501}],
        [{"bucket_id": "other", "allocation_bps": 10}],
        [
            {"bucket_id": "gamma", "allocation_bps": 10},
            {"bucket_id": "gamma", "allocation_bps": 10},
        ],
    ],
)
def test_ranged_pin_requires_an_in_range_allocation(allocations):
    with pytest.raises(ValidationError):
        enforcing(ranged(), allocations)


def test_fixed_pin_keeps_its_signed_allocation_and_pre_range_bytes():
    pin = enforcing(policy())
    assert pin.service_bps == 2500
    assert "allocations" not in pin.model_dump(mode="json")
    with pytest.raises(ValidationError):
        enforcing(policy(), [{"bucket_id": "gamma", "allocation_bps": 2500}])
