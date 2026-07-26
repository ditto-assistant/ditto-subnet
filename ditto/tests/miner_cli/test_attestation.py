"""Unit tests for :mod:`ditto.miner_cli.attestation`.

The platform verifier (``ditto/api_server/attestation.py`` in ditto-platform)
rebuilds these exact bytes and calls ``Keypair(ss58_address=...).verify`` on
them. Nothing in the wire format is negotiated at runtime, so the only way to
catch drift is to pin it:

- The payload bytes are exactly the domain-tagged tuple, in order, with
  ``issued_at`` rendered as an ISO-8601 UTC timestamp with microsecond
  precision.
- The pair is canonically sorted, so submitting the two hotkeys in either
  order yields the same ``(lo, hi)`` and the same signable bytes.
- ``side`` and ``key_kind`` are inside the signed bytes, so a ``lo`` half does
  not verify as a ``hi`` half and a hotkey-kind half is not a coldkey-kind
  half over the same side.
- Signatures are 128-char lowercase hex that round-trips through the
  server-side verifier flow, using real ``bittensor`` keypairs (//Alice and
  //Bob as hotkeys, //Charlie as an owning coldkey). No network, no keyfiles.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import bittensor

from ditto.miner_cli.attestation import (
    LINK_DOMAIN,
    MAX_LINK_DEPTH,
    canonical_pair,
    link_message,
    sign_link_half,
    signer_address,
)

NETUID = 118
NONCE = UUID("2f1d5f7a-8c4b-4a2e-9f01-6b3c8d5e7a90")
ISSUED_AT = datetime(2026, 7, 26, 15, 4, 5, 123456, tzinfo=UTC)
ISSUED_STR = "2026-07-26T15:04:05.123456+00:00"


def _alice() -> bittensor.Keypair:
    """Deterministic //Alice keypair standing in for one hotkey."""
    return bittensor.Keypair.create_from_uri("//Alice")


def _bob() -> bittensor.Keypair:
    """Deterministic //Bob keypair standing in for the other hotkey."""
    return bittensor.Keypair.create_from_uri("//Bob")


def _charlie() -> bittensor.Keypair:
    """Deterministic //Charlie keypair standing in for an owning coldkey."""
    return bittensor.Keypair.create_from_uri("//Charlie")


class _LiveWallet:
    """Wallet shim exposing only what the signers touch.

    ``coldkeypub`` is the public half the bittensor SDK exposes without a
    password; ``coldkey`` is the decrypted signing key. Both are present here
    so the "resolve the address without decrypting" path is exercised.
    """

    def __init__(
        self,
        hotkey: bittensor.Keypair,
        coldkey: bittensor.Keypair | None = None,
    ) -> None:
        self.hotkey = hotkey
        self.coldkey = coldkey
        self.coldkeypub = coldkey


class TestCanonicalPair:
    def test_sorts_lexicographically(self) -> None:
        assert canonical_pair("5AAA", "5BBB") == ("5AAA", "5BBB")
        assert canonical_pair("5BBB", "5AAA") == ("5AAA", "5BBB")

    def test_input_order_does_not_change_the_pair(self) -> None:
        """The link is symmetric: the same two miners must not be able to mint
        two distinct edges by submitting the pair in either order."""
        alice = _alice().ss58_address
        bob = _bob().ss58_address

        assert canonical_pair(alice, bob) == canonical_pair(bob, alice)

    def test_input_order_does_not_change_the_signed_bytes(self) -> None:
        alice = _alice().ss58_address
        bob = _bob().ss58_address

        forward_lo, forward_hi = canonical_pair(alice, bob)
        reverse_lo, reverse_hi = canonical_pair(bob, alice)

        common = {
            "netuid": NETUID,
            "nonce": NONCE,
            "issued_at": ISSUED_AT,
            "side": "lo",
            "key_kind": "hotkey",
            "signer": forward_lo,
        }
        assert link_message(
            hotkey_lo=forward_lo,
            hotkey_hi=forward_hi,
            **common,  # type: ignore[arg-type]
        ) == link_message(
            hotkey_lo=reverse_lo,
            hotkey_hi=reverse_hi,
            **common,  # type: ignore[arg-type]
        )


class TestLinkMessage:
    def test_payload_is_the_exact_domain_tagged_tuple(self) -> None:
        payload = link_message(
            netuid=NETUID,
            hotkey_lo="5LO",
            hotkey_hi="5HI",
            nonce=NONCE,
            issued_at=ISSUED_AT,
            side="lo",
            key_kind="hotkey",
            signer="5LO",
        )

        assert (
            payload
            == (
                f"ditto-owner-link:v1:118:5LO:5HI:{NONCE}:{ISSUED_STR}:lo:hotkey:5LO"
            ).encode()
        )

    def test_domain_tag_constant_matches_the_platform(self) -> None:
        assert LINK_DOMAIN == "ditto-owner-link:v1"

    def test_links_are_direct_only(self) -> None:
        """Pinned so the CLI's docs cannot start promising transitivity the
        platform does not honour."""
        assert MAX_LINK_DEPTH == 1

    def test_issued_at_is_normalised_to_utc_microseconds(self) -> None:
        """A miner in a non-UTC timezone must produce the same bytes as one in
        UTC for the same instant; the verifier only ever rebuilds the UTC
        form."""
        elsewhere = ISSUED_AT.astimezone(timezone(timedelta(hours=-7)))
        assert elsewhere.tzinfo != ISSUED_AT.tzinfo

        common = {
            "netuid": NETUID,
            "hotkey_lo": "5LO",
            "hotkey_hi": "5HI",
            "nonce": NONCE,
            "side": "lo",
            "key_kind": "hotkey",
            "signer": "5LO",
        }
        assert link_message(
            issued_at=elsewhere,
            **common,  # type: ignore[arg-type]
        ) == link_message(
            issued_at=ISSUED_AT,
            **common,  # type: ignore[arg-type]
        )

    def test_side_is_bound_into_the_bytes(self) -> None:
        common = {
            "netuid": NETUID,
            "hotkey_lo": "5LO",
            "hotkey_hi": "5HI",
            "nonce": NONCE,
            "issued_at": ISSUED_AT,
            "key_kind": "hotkey",
            "signer": "5LO",
        }
        lo = link_message(side="lo", **common)  # type: ignore[arg-type]
        hi = link_message(side="hi", **common)  # type: ignore[arg-type]

        assert lo != hi

    def test_key_kind_is_bound_into_the_bytes(self) -> None:
        """A coldkey proof must not be presentable as the stronger-looking
        hotkey proof (or vice versa) over identical bytes."""
        common = {
            "netuid": NETUID,
            "hotkey_lo": "5LO",
            "hotkey_hi": "5HI",
            "nonce": NONCE,
            "issued_at": ISSUED_AT,
            "side": "lo",
            "signer": "5SIGNER",
        }
        as_hotkey = link_message(key_kind="hotkey", **common)  # type: ignore[arg-type]
        as_coldkey = link_message(key_kind="coldkey", **common)  # type: ignore[arg-type]

        assert as_hotkey != as_coldkey

    def test_signer_is_bound_into_the_bytes(self) -> None:
        common = {
            "netuid": NETUID,
            "hotkey_lo": "5LO",
            "hotkey_hi": "5HI",
            "nonce": NONCE,
            "issued_at": ISSUED_AT,
            "side": "lo",
            "key_kind": "coldkey",
        }
        assert link_message(signer="5CK1", **common) != link_message(  # type: ignore[arg-type]
            signer="5CK2",
            **common,  # type: ignore[arg-type]
        )


class TestSignerAddress:
    def test_hotkey_kind_resolves_to_the_hotkey(self) -> None:
        wallet = _LiveWallet(_alice(), _charlie())

        assert (
            signer_address(live_wallet=wallet, key_kind="hotkey")  # type: ignore[arg-type]
            == _alice().ss58_address
        )

    def test_coldkey_kind_resolves_to_the_coldkey_public_half(self) -> None:
        """Resolving the address must not need the password; only signing
        does."""
        wallet = _LiveWallet(_alice(), _charlie())

        assert (
            signer_address(live_wallet=wallet, key_kind="coldkey")  # type: ignore[arg-type]
            == _charlie().ss58_address
        )


class TestSignLinkHalf:
    def test_returns_lowercase_128_hex_signature(self) -> None:
        alice, bob = _alice(), _bob()
        hotkey_lo, hotkey_hi = canonical_pair(alice.ss58_address, bob.ss58_address)

        sig_hex = sign_link_half(
            live_wallet=_LiveWallet(alice),  # type: ignore[arg-type]
            netuid=NETUID,
            hotkey_lo=hotkey_lo,
            hotkey_hi=hotkey_hi,
            nonce=NONCE,
            issued_at=ISSUED_AT,
            side="lo" if alice.ss58_address == hotkey_lo else "hi",
            key_kind="hotkey",
        )

        assert len(sig_hex) == 128
        assert sig_hex == sig_hex.lower()
        bytes.fromhex(sig_hex)

    def test_hotkey_half_round_trips_through_the_server_verifier(self) -> None:
        alice, bob = _alice(), _bob()
        hotkey_lo, hotkey_hi = canonical_pair(alice.ss58_address, bob.ss58_address)
        side = "lo" if alice.ss58_address == hotkey_lo else "hi"

        sig_hex = sign_link_half(
            live_wallet=_LiveWallet(alice),  # type: ignore[arg-type]
            netuid=NETUID,
            hotkey_lo=hotkey_lo,
            hotkey_hi=hotkey_hi,
            nonce=NONCE,
            issued_at=ISSUED_AT,
            side=side,  # type: ignore[arg-type]
            key_kind="hotkey",
        )

        # Mirror the platform's verify_signature verbatim.
        server_payload = (
            f"ditto-owner-link:v1:{NETUID}:{hotkey_lo}:{hotkey_hi}:{NONCE}:"
            f"{ISSUED_STR}:{side}:hotkey:{alice.ss58_address}"
        ).encode()
        verifier = bittensor.Keypair(ss58_address=alice.ss58_address)
        assert verifier.verify(server_payload, bytes.fromhex(sig_hex)) is True

    def test_coldkey_half_is_signed_by_the_coldkey(self) -> None:
        """The recovery path: the hotkey's key is unavailable, so the coldkey
        that owns it proves the half. The signature verifies against the
        COLDKEY, and the payload names it as the signer."""
        alice, bob, charlie = _alice(), _bob(), _charlie()
        hotkey_lo, hotkey_hi = canonical_pair(alice.ss58_address, bob.ss58_address)
        side = "lo" if alice.ss58_address == hotkey_lo else "hi"

        sig_hex = sign_link_half(
            live_wallet=_LiveWallet(alice, charlie),  # type: ignore[arg-type]
            netuid=NETUID,
            hotkey_lo=hotkey_lo,
            hotkey_hi=hotkey_hi,
            nonce=NONCE,
            issued_at=ISSUED_AT,
            side=side,  # type: ignore[arg-type]
            key_kind="coldkey",
        )

        server_payload = (
            f"ditto-owner-link:v1:{NETUID}:{hotkey_lo}:{hotkey_hi}:{NONCE}:"
            f"{ISSUED_STR}:{side}:coldkey:{charlie.ss58_address}"
        ).encode()
        assert (
            bittensor.Keypair(ss58_address=charlie.ss58_address).verify(
                server_payload, bytes.fromhex(sig_hex)
            )
            is True
        )
        # ...and it is emphatically not the hotkey's signature.
        assert (
            bittensor.Keypair(ss58_address=alice.ss58_address).verify(
                server_payload, bytes.fromhex(sig_hex)
            )
            is False
        )

    def test_signing_never_touches_a_transfer_path(self) -> None:
        """A coldkey proof decrypts a key to sign a string. Nothing on the
        wallet that could move funds is reachable, and the shim proves it: the
        only attributes the signer touches are the keypairs themselves."""
        alice, bob, charlie = _alice(), _bob(), _charlie()
        hotkey_lo, hotkey_hi = canonical_pair(alice.ss58_address, bob.ss58_address)

        # _LiveWallet has no transfer surface at all; if sign_link_half
        # reached for one this would raise AttributeError.
        sign_link_half(
            live_wallet=_LiveWallet(alice, charlie),  # type: ignore[arg-type]
            netuid=NETUID,
            hotkey_lo=hotkey_lo,
            hotkey_hi=hotkey_hi,
            nonce=NONCE,
            issued_at=ISSUED_AT,
            side="lo" if alice.ss58_address == hotkey_lo else "hi",
            key_kind="coldkey",
        )


class TestHalvesAreNotInterchangeable:
    def test_a_lo_half_does_not_verify_as_a_hi_half(self) -> None:
        """``side`` is inside the signed bytes so a half cannot be relabelled
        or moved onto the other endpoint."""
        alice, bob = _alice(), _bob()
        hotkey_lo, hotkey_hi = canonical_pair(alice.ss58_address, bob.ss58_address)
        lo_keypair = alice if alice.ss58_address == hotkey_lo else bob

        sig_hex = sign_link_half(
            live_wallet=_LiveWallet(lo_keypair),  # type: ignore[arg-type]
            netuid=NETUID,
            hotkey_lo=hotkey_lo,
            hotkey_hi=hotkey_hi,
            nonce=NONCE,
            issued_at=ISSUED_AT,
            side="lo",
            key_kind="hotkey",
        )

        common = {
            "netuid": NETUID,
            "hotkey_lo": hotkey_lo,
            "hotkey_hi": hotkey_hi,
            "nonce": NONCE,
            "issued_at": ISSUED_AT,
            "key_kind": "hotkey",
            "signer": lo_keypair.ss58_address,
        }
        verifier = bittensor.Keypair(ss58_address=lo_keypair.ss58_address)
        # Valid over its own side...
        assert (
            verifier.verify(
                link_message(side="lo", **common),  # type: ignore[arg-type]
                bytes.fromhex(sig_hex),
            )
            is True
        )
        # ...and worthless over the other side.
        assert (
            verifier.verify(
                link_message(side="hi", **common),  # type: ignore[arg-type]
                bytes.fromhex(sig_hex),
            )
            is False
        )

    def test_a_hotkey_half_does_not_verify_as_a_coldkey_half(self) -> None:
        """``key_kind`` is bound, so a hotkey proof cannot be re-presented as
        a coldkey proof over the same side."""
        alice, bob = _alice(), _bob()
        hotkey_lo, hotkey_hi = canonical_pair(alice.ss58_address, bob.ss58_address)
        side = "lo" if alice.ss58_address == hotkey_lo else "hi"

        sig_hex = sign_link_half(
            live_wallet=_LiveWallet(alice),  # type: ignore[arg-type]
            netuid=NETUID,
            hotkey_lo=hotkey_lo,
            hotkey_hi=hotkey_hi,
            nonce=NONCE,
            issued_at=ISSUED_AT,
            side=side,  # type: ignore[arg-type]
            key_kind="hotkey",
        )

        common = {
            "netuid": NETUID,
            "hotkey_lo": hotkey_lo,
            "hotkey_hi": hotkey_hi,
            "nonce": NONCE,
            "issued_at": ISSUED_AT,
            "side": side,
            "signer": alice.ss58_address,
        }
        verifier = bittensor.Keypair(ss58_address=alice.ss58_address)
        assert (
            verifier.verify(
                link_message(key_kind="hotkey", **common),  # type: ignore[arg-type]
                bytes.fromhex(sig_hex),
            )
            is True
        )
        assert (
            verifier.verify(
                link_message(key_kind="coldkey", **common),  # type: ignore[arg-type]
                bytes.fromhex(sig_hex),
            )
            is False
        )

    def test_a_different_nonce_invalidates_the_signature(self) -> None:
        """Replay guard: the nonce is inside the signed bytes, so the server
        cannot be handed a captured signature under a fresh nonce."""
        alice, bob = _alice(), _bob()
        hotkey_lo, hotkey_hi = canonical_pair(alice.ss58_address, bob.ss58_address)
        side = "lo" if alice.ss58_address == hotkey_lo else "hi"

        sig_hex = sign_link_half(
            live_wallet=_LiveWallet(alice),  # type: ignore[arg-type]
            netuid=NETUID,
            hotkey_lo=hotkey_lo,
            hotkey_hi=hotkey_hi,
            nonce=NONCE,
            issued_at=ISSUED_AT,
            side=side,  # type: ignore[arg-type]
            key_kind="hotkey",
        )

        other = link_message(
            netuid=NETUID,
            hotkey_lo=hotkey_lo,
            hotkey_hi=hotkey_hi,
            nonce=UUID("00000000-0000-4000-8000-000000000000"),
            issued_at=ISSUED_AT,
            side=side,  # type: ignore[arg-type]
            key_kind="hotkey",
            signer=alice.ss58_address,
        )
        verifier = bittensor.Keypair(ss58_address=alice.ss58_address)
        assert verifier.verify(other, bytes.fromhex(sig_hex)) is False

    def test_a_different_netuid_invalidates_the_signature(self) -> None:
        alice, bob = _alice(), _bob()
        hotkey_lo, hotkey_hi = canonical_pair(alice.ss58_address, bob.ss58_address)
        side = "lo" if alice.ss58_address == hotkey_lo else "hi"

        sig_hex = sign_link_half(
            live_wallet=_LiveWallet(alice),  # type: ignore[arg-type]
            netuid=NETUID,
            hotkey_lo=hotkey_lo,
            hotkey_hi=hotkey_hi,
            nonce=NONCE,
            issued_at=ISSUED_AT,
            side=side,  # type: ignore[arg-type]
            key_kind="hotkey",
        )

        other = link_message(
            netuid=NETUID + 1,
            hotkey_lo=hotkey_lo,
            hotkey_hi=hotkey_hi,
            nonce=NONCE,
            issued_at=ISSUED_AT,
            side=side,  # type: ignore[arg-type]
            key_kind="hotkey",
            signer=alice.ss58_address,
        )
        verifier = bittensor.Keypair(ss58_address=alice.ss58_address)
        assert verifier.verify(other, bytes.fromhex(sig_hex)) is False
