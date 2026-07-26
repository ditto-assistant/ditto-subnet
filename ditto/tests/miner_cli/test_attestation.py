"""Unit tests for :mod:`ditto.miner_cli.attestation`.

The platform verifier (``ditto/api_server/attestation.py`` in ditto-platform)
rebuilds these exact bytes and calls ``Keypair(ss58_address=...).verify`` on
them. Nothing in the wire format is negotiated at runtime, so the only way to
catch drift is to pin it:

- The payload bytes are exactly the domain-tagged tuple, in order, with
  ``issued_at`` rendered as an ISO-8601 UTC timestamp with microsecond
  precision.
- The attestation and acceptance halves differ ONLY in the domain tag, and a
  signature over one does not verify as the other (the cross-lane replay
  guard).
- Both signers produce 128-char lowercase hex that round-trips through the
  server-side verifier flow, using real ``bittensor`` keypairs (//Alice as the
  old hotkey, //Bob as the new one). No network, no keyfiles.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import bittensor

from ditto.miner_cli.attestation import (
    ACCEPTANCE_DOMAIN,
    ATTESTATION_DOMAIN,
    acceptance_message,
    attestation_message,
    sign_acceptance,
    sign_attestation,
)

NETUID = 118
NONCE = UUID("2f1d5f7a-8c4b-4a2e-9f01-6b3c8d5e7a90")
ISSUED_AT = datetime(2026, 7, 26, 15, 4, 5, 123456, tzinfo=UTC)
ISSUED_STR = "2026-07-26T15:04:05.123456+00:00"


def _old_keypair() -> bittensor.Keypair:
    """Deterministic //Alice keypair standing in for the OLD hotkey."""
    return bittensor.Keypair.create_from_uri("//Alice")


def _new_keypair() -> bittensor.Keypair:
    """Deterministic //Bob keypair standing in for the NEW hotkey."""
    return bittensor.Keypair.create_from_uri("//Bob")


class _LiveWallet:
    """Minimal wallet shim exposing only the ``hotkey`` the signers touch."""

    def __init__(self, keypair: bittensor.Keypair) -> None:
        self.hotkey = keypair


class TestAttestationMessage:
    def test_payload_is_the_exact_domain_tagged_tuple(self) -> None:
        payload = attestation_message(
            netuid=NETUID,
            old_hotkey="5OLD",
            new_hotkey="5NEW",
            nonce=NONCE,
            issued_at=ISSUED_AT,
        )

        assert (
            payload
            == (
                f"ditto-hotkey-attestation:v1:118:5OLD:5NEW:{NONCE}:{ISSUED_STR}"
            ).encode()
        )

    def test_issued_at_is_normalised_to_utc_microseconds(self) -> None:
        """A miner in a non-UTC timezone must produce the same bytes as one in
        UTC for the same instant; the verifier only ever rebuilds the UTC
        form."""
        elsewhere = ISSUED_AT.astimezone(timezone(timedelta(hours=-7)))
        assert elsewhere != ISSUED_AT or elsewhere.tzinfo != ISSUED_AT.tzinfo

        assert attestation_message(
            netuid=NETUID,
            old_hotkey="5OLD",
            new_hotkey="5NEW",
            nonce=NONCE,
            issued_at=elsewhere,
        ) == attestation_message(
            netuid=NETUID,
            old_hotkey="5OLD",
            new_hotkey="5NEW",
            nonce=NONCE,
            issued_at=ISSUED_AT,
        )

    def test_direction_is_signed_not_inferred(self) -> None:
        """Swapping the two hotkeys must change the bytes, or the edge would
        be symmetric and an attacker could point one at a victim."""
        forward = attestation_message(
            netuid=NETUID,
            old_hotkey="5OLD",
            new_hotkey="5NEW",
            nonce=NONCE,
            issued_at=ISSUED_AT,
        )
        reversed_ = attestation_message(
            netuid=NETUID,
            old_hotkey="5NEW",
            new_hotkey="5OLD",
            nonce=NONCE,
            issued_at=ISSUED_AT,
        )
        assert forward != reversed_


class TestAcceptanceMessage:
    def test_payload_is_the_exact_domain_tagged_tuple(self) -> None:
        payload = acceptance_message(
            netuid=NETUID,
            old_hotkey="5OLD",
            new_hotkey="5NEW",
            nonce=NONCE,
            issued_at=ISSUED_AT,
        )

        assert (
            payload
            == (
                f"ditto-hotkey-attestation-accept:v1:118:5OLD:5NEW:{NONCE}:{ISSUED_STR}"
            ).encode()
        )

    def test_halves_differ_only_by_domain_tag(self) -> None:
        kwargs = {
            "netuid": NETUID,
            "old_hotkey": "5OLD",
            "new_hotkey": "5NEW",
            "nonce": NONCE,
            "issued_at": ISSUED_AT,
        }
        attest = attestation_message(**kwargs)  # type: ignore[arg-type]
        accept = acceptance_message(**kwargs)  # type: ignore[arg-type]

        assert attest != accept
        assert attest.startswith(ATTESTATION_DOMAIN.encode() + b":")
        assert accept.startswith(ACCEPTANCE_DOMAIN.encode() + b":")
        # Strip the tags and the remainder must be byte-identical: the tuple
        # is shared, which is what makes the two halves inseparable.
        assert attest[len(ATTESTATION_DOMAIN) :] == accept[len(ACCEPTANCE_DOMAIN) :]


class TestSignAttestation:
    def test_returns_lowercase_128_hex_signature(self) -> None:
        sig_hex = sign_attestation(
            live_wallet=_LiveWallet(_old_keypair()),  # type: ignore[arg-type]
            netuid=NETUID,
            old_hotkey=_old_keypair().ss58_address,
            new_hotkey=_new_keypair().ss58_address,
            nonce=NONCE,
            issued_at=ISSUED_AT,
        )

        assert len(sig_hex) == 128
        assert sig_hex == sig_hex.lower()
        bytes.fromhex(sig_hex)

    def test_round_trips_through_the_server_verifier(self) -> None:
        old = _old_keypair()
        new = _new_keypair()

        sig_hex = sign_attestation(
            live_wallet=_LiveWallet(old),  # type: ignore[arg-type]
            netuid=NETUID,
            old_hotkey=old.ss58_address,
            new_hotkey=new.ss58_address,
            nonce=NONCE,
            issued_at=ISSUED_AT,
        )

        # Mirror the platform's verify_signature verbatim.
        server_payload = (
            f"ditto-hotkey-attestation:v1:{NETUID}:{old.ss58_address}:"
            f"{new.ss58_address}:{NONCE}:{ISSUED_STR}"
        ).encode()
        verifier = bittensor.Keypair(ss58_address=old.ss58_address)
        assert verifier.verify(server_payload, bytes.fromhex(sig_hex)) is True


class TestSignAcceptance:
    def test_returns_lowercase_128_hex_signature(self) -> None:
        sig_hex = sign_acceptance(
            live_wallet=_LiveWallet(_new_keypair()),  # type: ignore[arg-type]
            netuid=NETUID,
            old_hotkey=_old_keypair().ss58_address,
            new_hotkey=_new_keypair().ss58_address,
            nonce=NONCE,
            issued_at=ISSUED_AT,
        )

        assert len(sig_hex) == 128
        assert sig_hex == sig_hex.lower()
        bytes.fromhex(sig_hex)

    def test_round_trips_through_the_server_verifier(self) -> None:
        old = _old_keypair()
        new = _new_keypair()

        sig_hex = sign_acceptance(
            live_wallet=_LiveWallet(new),  # type: ignore[arg-type]
            netuid=NETUID,
            old_hotkey=old.ss58_address,
            new_hotkey=new.ss58_address,
            nonce=NONCE,
            issued_at=ISSUED_AT,
        )

        server_payload = (
            f"ditto-hotkey-attestation-accept:v1:{NETUID}:{old.ss58_address}:"
            f"{new.ss58_address}:{NONCE}:{ISSUED_STR}"
        ).encode()
        verifier = bittensor.Keypair(ss58_address=new.ss58_address)
        assert verifier.verify(server_payload, bytes.fromhex(sig_hex)) is True


class TestCrossLaneReplay:
    def test_attestation_signature_does_not_verify_as_an_acceptance(self) -> None:
        """The domain tags exist so a signature minted for one half cannot be
        lifted into the other. Pin that: sign the attestation with Alice, then
        try to verify it against the acceptance payload."""
        old = _old_keypair()
        new = _new_keypair()
        common = {
            "netuid": NETUID,
            "old_hotkey": old.ss58_address,
            "new_hotkey": new.ss58_address,
            "nonce": NONCE,
            "issued_at": ISSUED_AT,
        }

        sig_hex = sign_attestation(
            live_wallet=_LiveWallet(old),  # type: ignore[arg-type]
            **common,  # type: ignore[arg-type]
        )

        verifier = bittensor.Keypair(ss58_address=old.ss58_address)
        # Valid over its own payload...
        assert (
            verifier.verify(
                attestation_message(**common),  # type: ignore[arg-type]
                bytes.fromhex(sig_hex),
            )
            is True
        )
        # ...and worthless over the other lane's payload.
        assert (
            verifier.verify(
                acceptance_message(**common),  # type: ignore[arg-type]
                bytes.fromhex(sig_hex),
            )
            is False
        )

    def test_acceptance_signature_does_not_verify_as_an_attestation(self) -> None:
        old = _old_keypair()
        new = _new_keypair()
        common = {
            "netuid": NETUID,
            "old_hotkey": old.ss58_address,
            "new_hotkey": new.ss58_address,
            "nonce": NONCE,
            "issued_at": ISSUED_AT,
        }

        sig_hex = sign_acceptance(
            live_wallet=_LiveWallet(new),  # type: ignore[arg-type]
            **common,  # type: ignore[arg-type]
        )

        verifier = bittensor.Keypair(ss58_address=new.ss58_address)
        assert (
            verifier.verify(
                acceptance_message(**common),  # type: ignore[arg-type]
                bytes.fromhex(sig_hex),
            )
            is True
        )
        assert (
            verifier.verify(
                attestation_message(**common),  # type: ignore[arg-type]
                bytes.fromhex(sig_hex),
            )
            is False
        )

    def test_a_different_nonce_invalidates_the_signature(self) -> None:
        """Replay guard: the nonce is inside the signed bytes, so the server
        cannot be handed a captured signature under a fresh nonce."""
        old = _old_keypair()
        new = _new_keypair()

        sig_hex = sign_attestation(
            live_wallet=_LiveWallet(old),  # type: ignore[arg-type]
            netuid=NETUID,
            old_hotkey=old.ss58_address,
            new_hotkey=new.ss58_address,
            nonce=NONCE,
            issued_at=ISSUED_AT,
        )

        other = attestation_message(
            netuid=NETUID,
            old_hotkey=old.ss58_address,
            new_hotkey=new.ss58_address,
            nonce=UUID("00000000-0000-4000-8000-000000000000"),
            issued_at=ISSUED_AT,
        )
        verifier = bittensor.Keypair(ss58_address=old.ss58_address)
        assert verifier.verify(other, bytes.fromhex(sig_hex)) is False
