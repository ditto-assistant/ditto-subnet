from copy import deepcopy

import pytest
from pydantic import ValidationError

from ditto_screening_protocol.treasury import (
    TreasuryEmissionPolicy,
    TreasuryLedgerPin,
)


def pin_payload():
    policy = {
        "revision": 7,
        "genesis_hash": "0x" + "11" * 32,
        "collector_hotkey": "5" + "A" * 47,
        "collector_coldkey": "5" + "B" * 47,
        "collector_policy_digest": "cc" * 32,
        "buckets": [
            {
                "bucket_id": "gamma",
                "allocation_bps": 1000,
                "holding_coldkey": "5" + "C" * 47,
            }
        ],
    }
    return {
        "policy": policy,
        "policy_digest": TreasuryEmissionPolicy.model_validate(policy).digest,
        "identity": {
            "genesis_hash": policy["genesis_hash"],
            "finalized_block": 101,
            "finalized_block_hash": "0x" + "ab" * 32,
            "uid": 42,
            "hotkey": policy["collector_hotkey"],
            "owner_coldkey": policy["collector_coldkey"],
            "uid_hotkey": policy["collector_hotkey"],
            "subnet_owner_coldkey": "5" + "D" * 47,
        },
    }


def test_known_field_digest_and_immutable_bucket_order():
    raw = pin_payload()
    expected = TreasuryLedgerPin.model_validate(raw)
    raw["policy"]["future_field"] = {"service_bps": 10000}
    raw["identity"]["verified"] = True
    raw["future_authority"] = "enforce"
    pin = TreasuryLedgerPin.model_validate(raw)
    assert pin == expected
    assert pin.mode == "shadow"
    assert pin.policy.service_bps == 1000
    assert "future_field" not in pin.model_dump_json()
    raw["policy"]["buckets"][0]["allocation_bps"] = 0
    assert pin.policy.service_bps == 1000
    with pytest.raises(ValidationError):
        pin.policy.buckets[0].allocation_bps = 0


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("identity", "uid_hotkey", "5" + "E" * 47),
        ("identity", "owner_coldkey", "5" + "E" * 47),
        ("identity", "genesis_hash", "0x" + "22" * 32),
        ("identity", "netuid", 119),
        ("identity", "uid", True),
        ("identity", "uid", -1),
        ("identity", "uid", 65536),
        ("policy", "revision", 8),
        ("policy", "version", True),
        ("policy", "netuid", 118.0),
        ("policy", "collector_policy_digest", "short"),
    ],
)
def test_wrong_reused_uid_owner_chain_or_corrupt_policy_refused(section, field, value):
    raw = pin_payload()
    raw[section][field] = value
    with pytest.raises(ValidationError):
        TreasuryLedgerPin.model_validate(raw)


def test_owner_associated_collector_and_enforcement_are_refused():
    raw = pin_payload()
    raw["identity"]["subnet_owner_coldkey"] = raw["policy"]["collector_coldkey"]
    with pytest.raises(ValidationError, match="subnet owner"):
        TreasuryLedgerPin.model_validate(raw)
    raw = pin_payload()
    raw["mode"] = "enforce"
    with pytest.raises(ValidationError):
        TreasuryLedgerPin.model_validate(raw)


@pytest.mark.parametrize("block", [99, 102])
def test_identity_must_belong_to_pinned_epoch(block):
    raw = pin_payload()
    raw["identity"]["finalized_block"] = block
    pin = TreasuryLedgerPin.model_validate(raw)
    with pytest.raises(ValueError, match="stale or.*future"):
        pin.require_epoch(netuid=118, first_block=100, pinned_block=101)


@pytest.mark.parametrize(
    "change", ["overflow", "duplicate_id", "duplicate_wallet", "collector_wallet"]
)
def test_invalid_service_pool_rejected(change):
    raw = pin_payload()["policy"]
    bucket = deepcopy(raw["buckets"][0])
    if change == "overflow":
        bucket.update(
            bucket_id="beta", allocation_bps=1, holding_coldkey="5" + "E" * 47
        )
        raw["buckets"].append(bucket)
    elif change == "duplicate_id":
        bucket.update(allocation_bps=0, holding_coldkey="5" + "E" * 47)
        raw["buckets"].append(bucket)
    elif change == "duplicate_wallet":
        bucket.update(bucket_id="beta", allocation_bps=0)
        raw["buckets"].append(bucket)
    else:
        raw["buckets"][0]["holding_coldkey"] = raw["collector_coldkey"]
    with pytest.raises(ValidationError):
        TreasuryEmissionPolicy.model_validate(raw)
