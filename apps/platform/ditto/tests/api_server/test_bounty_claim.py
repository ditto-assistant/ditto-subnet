"""Unit tests for hotkey-signed bounty claims and verification logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import bittensor
import pytest

from ditto.api_models.bounty_claim import BountySpec
from ditto.api_server.bounty_claim import (
    BountyClaimRejected,
    bounty_appeal_message,
    bounty_claim_message,
    bounty_handoff_message,
    bounty_rebind_payee_message,
    bounty_renew_message,
    bounty_submit_message,
    bounty_team_member_message,
    bounty_withdraw_message,
    check_bounty_freshness,
    compute_spec_digest,
    compute_team_digest,
    verify_claimant_payee_binding,
    verify_signed_bounty_action,
)

_ISSUED = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
_EXPIRES = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
_NONCE = UUID("11111111-2222-3333-4444-555555555555")
_CLAIM_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def test_compute_spec_digest_deterministic() -> None:
    """Ensure spec digest computation is deterministic and field sensitive."""
    spec = BountySpec(
        scope="Implement hotkey-signed bounty claiming",
        acceptance_evidence="Unit tests passing with sr25519 signatures",
        reward_min_tao=5,
        reward_max_tao=25,
        reviewer="maintainer-1",
        dependencies=["#2044"],
        bounty_expires_at=_EXPIRES,
        concurrency="exclusive",
        max_open_claims=1,
        reservation_days=7,
        max_renewals=2,
        policy_revision=1,
    )
    digest1 = compute_spec_digest(spec)
    digest2 = compute_spec_digest(spec)
    assert digest1 == digest2
    assert len(digest1) == 64

    spec_mutated = spec.model_copy(update={"reward_max_tao": 30})
    assert compute_spec_digest(spec_mutated) != digest1


def test_compute_team_digest_order_independent() -> None:
    """Ensure team digest is independent of member list order."""
    hotkey_a = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    hotkey_b = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
    payee = "5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy"

    digest1 = compute_team_digest([hotkey_a, hotkey_b], payee)
    digest2 = compute_team_digest([hotkey_b, hotkey_a], payee)
    assert digest1 == digest2
    assert len(digest1) == 64


def test_claim_signature_round_trip() -> None:
    """Test standard round trip for hotkey-signed bounty claim."""
    alice = bittensor.Keypair.create_from_uri("//Alice")
    spec_digest = "a" * 64
    team_digest = "0" * 64

    payload = bounty_claim_message(
        netuid=118,
        repo="ditto-assistant/ditto-subnet",
        issue=2045,
        bounty_revision=1,
        spec_digest=spec_digest,
        claimant_hotkey=alice.ss58_address,
        payee_coldkey=alice.ss58_address,
        github_login="alice-contributor",
        team_digest=team_digest,
        expires_at=_EXPIRES,
        nonce=_NONCE,
        issued_at=_ISSUED,
        key_kind="hotkey",
        signer=alice.ss58_address,
    )
    sig = alice.sign(payload).hex()

    verify_signed_bounty_action(
        payload=payload,
        key_kind="hotkey",
        signer=alice.ss58_address,
        signature=sig,
        expected_hotkey=alice.ss58_address,
    )


def test_claim_signature_rejects_wrong_signer() -> None:
    """Test rejection when signature does not match claimant."""
    alice = bittensor.Keypair.create_from_uri("//Alice")
    bob = bittensor.Keypair.create_from_uri("//Bob")

    payload = bounty_claim_message(
        netuid=118,
        repo="ditto-assistant/ditto-subnet",
        issue=2045,
        bounty_revision=1,
        spec_digest="a" * 64,
        claimant_hotkey=alice.ss58_address,
        payee_coldkey=alice.ss58_address,
        github_login="alice-contributor",
        team_digest="0" * 64,
        expires_at=_EXPIRES,
        nonce=_NONCE,
        issued_at=_ISSUED,
        key_kind="hotkey",
        signer=alice.ss58_address,
    )
    sig_bob = bob.sign(payload).hex()

    with pytest.raises(BountyClaimRejected, match="signature did not verify"):
        verify_signed_bounty_action(
            payload=payload,
            key_kind="hotkey",
            signer=alice.ss58_address,
            signature=sig_bob,
            expected_hotkey=alice.ss58_address,
        )


def test_claim_signature_with_coldkey_signer() -> None:
    """Test claim signed by on-chain owner coldkey."""
    alice_hotkey = bittensor.Keypair.create_from_uri("//Alice")
    bob_coldkey = bittensor.Keypair.create_from_uri("//Bob")

    payload = bounty_claim_message(
        netuid=118,
        repo="ditto-assistant/ditto-subnet",
        issue=2045,
        bounty_revision=1,
        spec_digest="a" * 64,
        claimant_hotkey=alice_hotkey.ss58_address,
        payee_coldkey=bob_coldkey.ss58_address,
        github_login="alice-contributor",
        team_digest="0" * 64,
        expires_at=_EXPIRES,
        nonce=_NONCE,
        issued_at=_ISSUED,
        key_kind="coldkey",
        signer=bob_coldkey.ss58_address,
    )
    sig = bob_coldkey.sign(payload).hex()

    verify_signed_bounty_action(
        payload=payload,
        key_kind="coldkey",
        signer=bob_coldkey.ss58_address,
        signature=sig,
        expected_hotkey=alice_hotkey.ss58_address,
        expected_coldkey=bob_coldkey.ss58_address,
    )


def test_renew_signature_verification() -> None:
    """Test verification of bounty renewal action."""
    alice = bittensor.Keypair.create_from_uri("//Alice")
    new_expires = _EXPIRES + timedelta(days=7)

    payload = bounty_renew_message(
        netuid=118,
        claim_id=_CLAIM_ID,
        claimant_hotkey=alice.ss58_address,
        bounty_revision=1,
        spec_digest="a" * 64,
        new_expires_at=new_expires,
        nonce=_NONCE,
        issued_at=_ISSUED,
        key_kind="hotkey",
        signer=alice.ss58_address,
    )
    sig = alice.sign(payload).hex()

    verify_signed_bounty_action(
        payload=payload,
        key_kind="hotkey",
        signer=alice.ss58_address,
        signature=sig,
        expected_hotkey=alice.ss58_address,
    )


def test_submit_signature_verification() -> None:
    """Test verification of bounty submit action locking PR and head SHA."""
    alice = bittensor.Keypair.create_from_uri("//Alice")
    head_sha = "7c1e8b4f2a9d0e1c3b5a7f9e8d1c2b3a4f5e6d7a"

    payload = bounty_submit_message(
        netuid=118,
        claim_id=_CLAIM_ID,
        claimant_hotkey=alice.ss58_address,
        pr_number=2080,
        head_sha=head_sha,
        nonce=_NONCE,
        issued_at=_ISSUED,
        key_kind="hotkey",
        signer=alice.ss58_address,
    )
    sig = alice.sign(payload).hex()

    verify_signed_bounty_action(
        payload=payload,
        key_kind="hotkey",
        signer=alice.ss58_address,
        signature=sig,
        expected_hotkey=alice.ss58_address,
    )


def test_handoff_signature_verification() -> None:
    """Test two-sided handoff action signatures."""
    alice = bittensor.Keypair.create_from_uri("//Alice")
    bob = bittensor.Keypair.create_from_uri("//Bob")

    payload_from = bounty_handoff_message(
        netuid=118,
        claim_id=_CLAIM_ID,
        side="from",
        from_hotkey=alice.ss58_address,
        to_hotkey=bob.ss58_address,
        nonce=_NONCE,
        issued_at=_ISSUED,
        key_kind="hotkey",
        signer=alice.ss58_address,
    )
    sig_from = alice.sign(payload_from).hex()

    verify_signed_bounty_action(
        payload=payload_from,
        key_kind="hotkey",
        signer=alice.ss58_address,
        signature=sig_from,
        expected_hotkey=alice.ss58_address,
    )

    payload_to = bounty_handoff_message(
        netuid=118,
        claim_id=_CLAIM_ID,
        side="to",
        from_hotkey=alice.ss58_address,
        to_hotkey=bob.ss58_address,
        nonce=_NONCE,
        issued_at=_ISSUED,
        key_kind="hotkey",
        signer=bob.ss58_address,
    )
    sig_to = bob.sign(payload_to).hex()

    verify_signed_bounty_action(
        payload=payload_to,
        key_kind="hotkey",
        signer=bob.ss58_address,
        signature=sig_to,
        expected_hotkey=bob.ss58_address,
    )


def test_withdraw_signature_verification() -> None:
    """Test voluntary withdrawal action signature."""
    alice = bittensor.Keypair.create_from_uri("//Alice")

    payload = bounty_withdraw_message(
        netuid=118,
        claim_id=_CLAIM_ID,
        claimant_hotkey=alice.ss58_address,
        nonce=_NONCE,
        issued_at=_ISSUED,
        key_kind="hotkey",
        signer=alice.ss58_address,
    )
    sig = alice.sign(payload).hex()

    verify_signed_bounty_action(
        payload=payload,
        key_kind="hotkey",
        signer=alice.ss58_address,
        signature=sig,
        expected_hotkey=alice.ss58_address,
    )


def test_rebind_payee_signature_verification() -> None:
    """Test payee coldkey rebind signature verification."""
    alice_hotkey = bittensor.Keypair.create_from_uri("//Alice")
    old_coldkey = bittensor.Keypair.create_from_uri("//Bob")
    new_coldkey = bittensor.Keypair.create_from_uri("//Charlie")

    payload = bounty_rebind_payee_message(
        netuid=118,
        claim_id=_CLAIM_ID,
        claimant_hotkey=alice_hotkey.ss58_address,
        old_payee_coldkey=old_coldkey.ss58_address,
        new_payee_coldkey=new_coldkey.ss58_address,
        nonce=_NONCE,
        issued_at=_ISSUED,
        key_kind="coldkey",
        signer=new_coldkey.ss58_address,
    )
    sig = new_coldkey.sign(payload).hex()

    verify_signed_bounty_action(
        payload=payload,
        key_kind="coldkey",
        signer=new_coldkey.ss58_address,
        signature=sig,
        expected_hotkey=alice_hotkey.ss58_address,
        expected_coldkey=new_coldkey.ss58_address,
    )


def test_appeal_signature_verification() -> None:
    """Test appeal action signature verification."""
    alice = bittensor.Keypair.create_from_uri("//Alice")

    payload = bounty_appeal_message(
        netuid=118,
        claim_id=_CLAIM_ID,
        revocation_entry_hash="b" * 64,
        reason_digest="c" * 64,
        nonce=_NONCE,
        issued_at=_ISSUED,
        key_kind="hotkey",
        signer=alice.ss58_address,
    )
    sig = alice.sign(payload).hex()

    verify_signed_bounty_action(
        payload=payload,
        key_kind="hotkey",
        signer=alice.ss58_address,
        signature=sig,
        expected_hotkey=alice.ss58_address,
    )


def test_team_member_signature_verification() -> None:
    """Test team member individual attestation signature."""
    alice = bittensor.Keypair.create_from_uri("//Alice")

    payload = bounty_team_member_message(
        netuid=118,
        team_digest="d" * 64,
        member_hotkey=alice.ss58_address,
        nonce=_NONCE,
        issued_at=_ISSUED,
        key_kind="hotkey",
        signer=alice.ss58_address,
    )
    sig = alice.sign(payload).hex()

    verify_signed_bounty_action(
        payload=payload,
        key_kind="hotkey",
        signer=alice.ss58_address,
        signature=sig,
        expected_hotkey=alice.ss58_address,
    )


def test_freshness_check() -> None:
    """Test timestamp skew and maximum age checks."""
    now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    check_bounty_freshness(issued_at=now, now=now)
    check_bounty_freshness(issued_at=now - timedelta(hours=23), now=now)

    with pytest.raises(BountyClaimRejected, match="in the future"):
        check_bounty_freshness(issued_at=now + timedelta(minutes=6), now=now)

    with pytest.raises(BountyClaimRejected, match="has expired"):
        check_bounty_freshness(issued_at=now - timedelta(hours=25), now=now)


def test_verify_claimant_payee_binding() -> None:
    """Test binding between payee coldkey and Subtensor on-chain owner."""
    alice_hotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    bob_coldkey = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
    eve_coldkey = "5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy"

    verify_claimant_payee_binding(
        claimant_hotkey=alice_hotkey,
        payee_coldkey=bob_coldkey,
        on_chain_owner=bob_coldkey,
    )

    with pytest.raises(BountyClaimRejected, match="does not match on-chain owner"):
        verify_claimant_payee_binding(
            claimant_hotkey=alice_hotkey,
            payee_coldkey=eve_coldkey,
            on_chain_owner=bob_coldkey,
        )
