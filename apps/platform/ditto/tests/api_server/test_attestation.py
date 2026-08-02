"""Unit tests for the pure owner-link crypto/message layer.

These use real sr25519 keypairs from dev URIs, so a payload-format regression
that would silently break every miner's CLI fails here rather than in
production.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import bittensor
import pytest

from ditto.api_server.attestation import (
    MAX_LINK_DEPTH,
    AttestationRejected,
    canonical_pair,
    check_freshness,
    evidence_grade,
    link_message,
    verify_link,
    verify_signature,
)

_ISSUED = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)
_NONCE = UUID("11111111-2222-3333-4444-555555555555")


def _kp(uri: str) -> bittensor.Keypair:
    return bittensor.Keypair.create_from_uri(uri)


def _half(
    signer: bittensor.Keypair,
    *,
    hotkey_lo: str,
    hotkey_hi: str,
    side: str,
    key_kind: str = "hotkey",
    netuid: int = 118,
) -> str:
    return signer.sign(
        link_message(
            netuid=netuid,
            hotkey_lo=hotkey_lo,
            hotkey_hi=hotkey_hi,
            nonce=_NONCE,
            issued_at=_ISSUED,
            side=side,  # type: ignore[arg-type]
            key_kind=key_kind,  # type: ignore[arg-type]
            signer=signer.ss58_address,
        )
    ).hex()


def _msg(
    *,
    netuid: int = 118,
    hotkey_lo: str = "5Alpha",
    hotkey_hi: str = "5Beta",
    side: str = "lo",
    key_kind: str = "hotkey",
    signer: str = "5Alpha",
) -> bytes:
    """Build a payload with one field varied, for the "is bound" tests."""
    return link_message(
        netuid=netuid,
        hotkey_lo=hotkey_lo,
        hotkey_hi=hotkey_hi,
        nonce=_NONCE,
        issued_at=_ISSUED,
        side=side,  # type: ignore[arg-type]
        key_kind=key_kind,  # type: ignore[arg-type]
        signer=signer,
    )


def _link(
    a: bittensor.Keypair,
    b: bittensor.Keypair,
    *,
    netuid: int = 118,
) -> dict:
    """A well-formed hotkey-proved link between two keypairs."""
    lo, hi = canonical_pair(a.ss58_address, b.ss58_address)
    lo_kp, hi_kp = (a, b) if a.ss58_address == lo else (b, a)
    return {
        "netuid": netuid,
        "hotkey_lo": lo,
        "hotkey_hi": hi,
        "nonce": _NONCE,
        "issued_at": _ISSUED,
        "lo_key_kind": "hotkey",
        "lo_signer": lo,
        "lo_signature": _half(
            lo_kp, hotkey_lo=lo, hotkey_hi=hi, side="lo", netuid=netuid
        ),
        "hi_key_kind": "hotkey",
        "hi_signer": hi,
        "hi_signature": _half(
            hi_kp, hotkey_lo=lo, hotkey_hi=hi, side="hi", netuid=netuid
        ),
        "lo_bound_coldkey": None,
        "hi_bound_coldkey": None,
    }


class TestCanonicalPair:
    def test_sorts(self) -> None:
        assert canonical_pair("5B", "5A") == ("5A", "5B")
        assert canonical_pair("5A", "5B") == ("5A", "5B")

    def test_order_of_arguments_is_irrelevant(self) -> None:
        """The link is symmetric, so a pair has one representation."""
        assert canonical_pair("5A", "5B") == canonical_pair("5B", "5A")


class TestSignedPayload:
    def test_payload_is_pinned(self) -> None:
        """The exact bytes are a cross-repo contract with the miner CLI."""
        msg = link_message(
            netuid=118,
            hotkey_lo="5Alpha",
            hotkey_hi="5Beta",
            nonce=_NONCE,
            issued_at=_ISSUED,
            side="lo",
            key_kind="hotkey",
            signer="5Alpha",
        )
        assert msg == (
            b"ditto-owner-link:v1:118:5Alpha:5Beta:"
            b"11111111-2222-3333-4444-555555555555:"
            b"2026-07-26T12:00:00.000000+00:00:lo:hotkey:5Alpha"
        )

    def test_side_is_bound(self) -> None:
        """A half cannot be replayed onto the other endpoint."""
        assert _msg(side="lo") != _msg(side="hi")

    def test_key_kind_is_bound(self) -> None:
        """A hotkey proof cannot be relabelled as the stronger coldkey proof."""
        assert _msg(key_kind="hotkey") != _msg(key_kind="coldkey")

    def test_signer_is_bound(self) -> None:
        assert _msg(signer="5ColdA") != _msg(signer="5ColdB")

    def test_netuid_is_bound(self) -> None:
        """Cross-subnet reuse is impossible: the netuid is inside the bytes."""
        assert _msg(netuid=118) != _msg(netuid=64)

    def test_pair_is_bound(self) -> None:
        assert _msg(hotkey_hi="5Beta") != _msg(hotkey_hi="5Gamma")


class TestEvidenceGrade:
    def test_grades(self) -> None:
        assert evidence_grade("coldkey", "coldkey") == "coldkey-coldkey"
        assert evidence_grade("hotkey", "hotkey") == "hotkey-hotkey"
        assert evidence_grade("hotkey", "coldkey") == "mixed"
        assert evidence_grade("coldkey", "hotkey") == "mixed"


class TestVerification:
    def test_valid_hotkey_proved_link_verifies(self) -> None:
        verify_link(**_link(_kp("//Alice"), _kp("//Bob")), now=_ISSUED)

    def test_coldkey_proved_half_verifies_against_payment_binding(self) -> None:
        """A coldkey proof is checked against a binding the platform knows
        independently of the attestation."""
        alice, bob, cold = _kp("//Alice"), _kp("//Bob"), _kp("//ColdOwner")
        lo, hi = canonical_pair(alice.ss58_address, bob.ss58_address)
        lo_kp, hi_kp = (alice, bob) if alice.ss58_address == lo else (bob, alice)
        verify_link(
            netuid=118,
            hotkey_lo=lo,
            hotkey_hi=hi,
            nonce=_NONCE,
            issued_at=_ISSUED,
            lo_key_kind="coldkey",
            lo_signer=cold.ss58_address,
            lo_signature=_half(
                cold, hotkey_lo=lo, hotkey_hi=hi, side="lo", key_kind="coldkey"
            ),
            hi_key_kind="hotkey",
            hi_signer=hi,
            hi_signature=_half(hi_kp, hotkey_lo=lo, hotkey_hi=hi, side="hi"),
            lo_bound_coldkey=cold.ss58_address,
            hi_bound_coldkey=None,
            now=_ISSUED,
        )
        assert lo_kp is not None  # the lo hotkey never signed; the coldkey did

    def test_coldkey_half_rejected_when_binding_absent(self) -> None:
        """No payment record means no verifiable coldkey->hotkey binding."""
        alice, bob, cold = _kp("//Alice"), _kp("//Bob"), _kp("//ColdOwner")
        lo, hi = canonical_pair(alice.ss58_address, bob.ss58_address)
        hi_kp = bob if bob.ss58_address == hi else alice
        with pytest.raises(AttestationRejected, match="no payment record"):
            verify_link(
                netuid=118,
                hotkey_lo=lo,
                hotkey_hi=hi,
                nonce=_NONCE,
                issued_at=_ISSUED,
                lo_key_kind="coldkey",
                lo_signer=cold.ss58_address,
                lo_signature=_half(
                    cold, hotkey_lo=lo, hotkey_hi=hi, side="lo", key_kind="coldkey"
                ),
                hi_key_kind="hotkey",
                hi_signer=hi,
                hi_signature=_half(hi_kp, hotkey_lo=lo, hotkey_hi=hi, side="hi"),
                lo_bound_coldkey=None,
                hi_bound_coldkey=None,
                now=_ISSUED,
            )

    def test_coldkey_half_rejected_when_binding_disagrees(self) -> None:
        """A stale owner cannot sign for a hotkey that has since moved."""
        alice, bob = _kp("//Alice"), _kp("//Bob")
        stale, current = _kp("//StaleOwner"), _kp("//CurrentOwner")
        lo, hi = canonical_pair(alice.ss58_address, bob.ss58_address)
        hi_kp = bob if bob.ss58_address == hi else alice
        with pytest.raises(AttestationRejected, match="does not own"):
            verify_link(
                netuid=118,
                hotkey_lo=lo,
                hotkey_hi=hi,
                nonce=_NONCE,
                issued_at=_ISSUED,
                lo_key_kind="coldkey",
                lo_signer=stale.ss58_address,
                lo_signature=_half(
                    stale, hotkey_lo=lo, hotkey_hi=hi, side="lo", key_kind="coldkey"
                ),
                hi_key_kind="hotkey",
                hi_signer=hi,
                hi_signature=_half(hi_kp, hotkey_lo=lo, hotkey_hi=hi, side="hi"),
                lo_bound_coldkey=current.ss58_address,
                hi_bound_coldkey=None,
                now=_ISSUED,
            )

    def test_hotkey_half_must_be_signed_by_that_hotkey(self) -> None:
        payload = _link(_kp("//Alice"), _kp("//Bob"))
        payload["lo_signer"] = _kp("//Eve").ss58_address
        with pytest.raises(AttestationRejected, match="signer is not"):
            verify_link(**payload, now=_ISSUED)

    def test_forged_signature_is_rejected(self) -> None:
        alice, bob, eve = _kp("//Alice"), _kp("//Bob"), _kp("//Eve")
        payload = _link(alice, bob)
        lo, hi = payload["hotkey_lo"], payload["hotkey_hi"]
        # Eve signs the lo half but declares the real lo hotkey as signer.
        payload["lo_signature"] = eve.sign(
            link_message(
                netuid=118,
                hotkey_lo=lo,
                hotkey_hi=hi,
                nonce=_NONCE,
                issued_at=_ISSUED,
                side="lo",
                key_kind="hotkey",
                signer=lo,
            )
        ).hex()
        with pytest.raises(AttestationRejected, match="did not verify"):
            verify_link(**payload, now=_ISSUED)

    def test_garbage_signature_is_rejected(self) -> None:
        payload = _link(_kp("//Alice"), _kp("//Bob"))
        payload["hi_signature"] = "ff" * 64
        with pytest.raises(AttestationRejected, match="did not verify"):
            verify_link(**payload, now=_ISSUED)

    def test_half_cannot_be_moved_to_the_other_side(self) -> None:
        """`side` is signed, so the lo proof is not a valid hi proof."""
        alice, bob = _kp("//Alice"), _kp("//Bob")
        payload = _link(alice, bob)
        lo, hi = payload["hotkey_lo"], payload["hotkey_hi"]
        hi_kp = bob if bob.ss58_address == hi else alice
        payload["hi_signature"] = _half(hi_kp, hotkey_lo=lo, hotkey_hi=hi, side="lo")
        with pytest.raises(AttestationRejected, match="did not verify"):
            verify_link(**payload, now=_ISSUED)

    def test_third_party_cannot_mint_a_link_at_a_victims_hotkey(self) -> None:
        """THE load-bearing abuse case.

        Mallory wants copy screening suppressed between her hotkey and a
        victim's so she can resubmit the victim's work. She controls only her
        own key, and the link suppresses screening between its two endpoints
        regardless of order -- so requiring *both* halves is what stops her.
        She can produce her own half and never the victim's.
        """
        mallory, victim = _kp("//Mallory"), _kp("//Victim")
        lo, hi = canonical_pair(mallory.ss58_address, victim.ss58_address)
        mallory_side = "lo" if mallory.ss58_address == lo else "hi"
        victim_side = "hi" if mallory_side == "lo" else "lo"

        forged: dict = {
            "netuid": 118,
            "hotkey_lo": lo,
            "hotkey_hi": hi,
            "nonce": _NONCE,
            "issued_at": _ISSUED,
            f"{mallory_side}_key_kind": "hotkey",
            f"{mallory_side}_signer": mallory.ss58_address,
            f"{mallory_side}_signature": _half(
                mallory, hotkey_lo=lo, hotkey_hi=hi, side=mallory_side
            ),
            # She signs the victim's half too -- the only key she has.
            f"{victim_side}_key_kind": "hotkey",
            f"{victim_side}_signer": victim.ss58_address,
            f"{victim_side}_signature": mallory.sign(
                link_message(
                    netuid=118,
                    hotkey_lo=lo,
                    hotkey_hi=hi,
                    nonce=_NONCE,
                    issued_at=_ISSUED,
                    side=victim_side,  # type: ignore[arg-type]
                    key_kind="hotkey",
                    signer=victim.ss58_address,
                )
            ).hex(),
            "lo_bound_coldkey": None,
            "hi_bound_coldkey": None,
        }
        with pytest.raises(AttestationRejected, match="did not verify"):
            verify_link(**forged, now=_ISSUED)

    def test_self_link_is_rejected(self) -> None:
        alice = _kp("//Alice")
        with pytest.raises(AttestationRejected, match="must differ"):
            verify_link(
                netuid=118,
                hotkey_lo=alice.ss58_address,
                hotkey_hi=alice.ss58_address,
                nonce=_NONCE,
                issued_at=_ISSUED,
                lo_key_kind="hotkey",
                lo_signer=alice.ss58_address,
                lo_signature="ab" * 64,
                hi_key_kind="hotkey",
                hi_signer=alice.ss58_address,
                hi_signature="ab" * 64,
                lo_bound_coldkey=None,
                hi_bound_coldkey=None,
                now=_ISSUED,
            )

    def test_non_canonical_order_is_rejected(self) -> None:
        payload = _link(_kp("//Alice"), _kp("//Bob"))
        payload["hotkey_lo"], payload["hotkey_hi"] = (
            payload["hotkey_hi"],
            payload["hotkey_lo"],
        )
        with pytest.raises(AttestationRejected, match="canonical order"):
            verify_link(**payload, now=_ISSUED)

    def test_wrong_netuid_is_rejected(self) -> None:
        payload = _link(_kp("//Alice"), _kp("//Bob"), netuid=64)
        with pytest.raises(AttestationRejected, match="netuid"):
            verify_link(**payload, now=_ISSUED)

    def test_verify_signature_is_false_not_raising_on_malformed_input(self) -> None:
        assert (
            verify_signature(signer="not-ss58", payload=b"x", signature_hex="zz")
            is False
        )


class TestFreshness:
    def test_future_beyond_skew_is_rejected(self) -> None:
        with pytest.raises(AttestationRejected, match="future"):
            check_freshness(issued_at=_ISSUED + timedelta(hours=1), now=_ISSUED)

    def test_expired_is_rejected(self) -> None:
        with pytest.raises(AttestationRejected, match="expired"):
            check_freshness(issued_at=_ISSUED, now=_ISSUED + timedelta(days=2))

    def test_small_clock_skew_is_tolerated(self) -> None:
        check_freshness(issued_at=_ISSUED + timedelta(minutes=1), now=_ISSUED)


class TestLinkDepth:
    def test_links_are_direct_only(self) -> None:
        """Pinned as a constant so the non-transitivity choice is explicit.

        Symmetric edges plus transitivity would let an intermediary bridge two
        owners who never signed anything with each other.
        """
        assert MAX_LINK_DEPTH == 1
