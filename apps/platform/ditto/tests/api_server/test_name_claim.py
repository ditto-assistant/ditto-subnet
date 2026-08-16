"""Pure tests for handle-stem normalization and signed claim payloads."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import bittensor
import pytest

from ditto.api_server.name_claim import (
    NameClaimRejected,
    claim_message,
    handle_status_for,
    normalize_name_stem,
    require_name_stem,
    verify_signed_action,
)

_ISSUED = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
_NONCE = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


@pytest.mark.parametrize(
    ("name", "stem"),
    [
        ("Jupiter-ditto-v10", "jupiter"),
        ("jupiter", "jupiter"),
        ("JUPITER_v2", "jupiter"),
        ("Omar-miner", "omar"),
        ("kaelith-ditto-miner", "kaelith"),
        ("Hannibal-ditto-v10-gate", "hannibal-gate"),
        ("Crown-v10-v5", "crown"),
        ("lihai_v10_7", "lihai"),
        ("harry.lii", "harry-lii"),
        ("red-dragon", "red-dragon"),
        ("luffy10", "luffy10"),
    ],
)
def test_normalize_name_stem(name: str, stem: str) -> None:
    assert normalize_name_stem(name) == stem


def test_require_name_stem_rejects_filler_only() -> None:
    with pytest.raises(NameClaimRejected):
        require_name_stem("ditto-v10")


def test_claim_signature_round_trip() -> None:
    kp = bittensor.Keypair.create_from_uri("//Alice")
    payload = claim_message(
        netuid=118,
        name_stem="jupiter",
        claimant_hotkey=kp.ss58_address,
        nonce=_NONCE,
        issued_at=_ISSUED,
        key_kind="hotkey",
        signer=kp.ss58_address,
    )
    signature = kp.sign(payload).hex()
    verify_signed_action(
        payload=payload,
        hotkey=kp.ss58_address,
        key_kind="hotkey",
        signer=kp.ss58_address,
        signature=signature,
        bound_coldkey=None,
    )


def test_claim_signature_rejects_wrong_signer() -> None:
    alice = bittensor.Keypair.create_from_uri("//Alice")
    bob = bittensor.Keypair.create_from_uri("//Bob")
    payload = claim_message(
        netuid=118,
        name_stem="jupiter",
        claimant_hotkey=alice.ss58_address,
        nonce=_NONCE,
        issued_at=_ISSUED,
        key_kind="hotkey",
        signer=alice.ss58_address,
    )
    signature = bob.sign(payload).hex()
    with pytest.raises(NameClaimRejected, match="did not verify"):
        verify_signed_action(
            payload=payload,
            hotkey=alice.ss58_address,
            key_kind="hotkey",
            signer=alice.ss58_address,
            signature=signature,
            bound_coldkey=None,
        )


def test_handle_status_classifies_reserved_and_disputed() -> None:
    claims = {"jupiter": ("upheld", "coldkey:owner-a")}
    assert (
        handle_status_for(
            agent_name="Jupiter-ditto-v10",
            owner_root="coldkey:owner-a",
            claims=claims,
        )
        == "reserved"
    )
    assert (
        handle_status_for(
            agent_name="jupiter",
            owner_root="coldkey:thief",
            claims=claims,
        )
        == "disputed"
    )
    assert (
        handle_status_for(
            agent_name="red-dragon",
            owner_root="coldkey:thief",
            claims=claims,
        )
        is None
    )
