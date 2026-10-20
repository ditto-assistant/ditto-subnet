"""Unit tests for :mod:`ditto.miner_cli.signing`.

Three invariants pinned:

- The payload bytes include the domain, hotkey, digest, timestamp, and nonce
  (matches the server's ``_verify_signature`` at
  ``ditto/api_server/endpoints/upload.py:198``).
- ``sign_upload_payload`` returns a 128-hex string matching the
  server's ``_SIGNATURE_HEX_PATTERN``.
- A CLI-produced signature round-trips through the server-side
  ``bittensor.Keypair.verify`` flow (true end-to-end sig contract check
  using the real bittensor library; no network).
"""

from __future__ import annotations

from uuid import UUID

import bittensor

from ditto.miner_cli.models import WalletHandle
from ditto.miner_cli.signing import (
    build_upload_payload,
    sign_upload_payload,
)

_NONCE = UUID("123e4567-e89b-42d3-a456-426614174000")
_TIMESTAMP = 1_798_000_000


def _make_test_keypair() -> bittensor.Keypair:
    """Deterministic Alice keypair via the standard substrate dev URI.

    No network. No keyfile on disk. Same keypair every run.
    """
    return bittensor.Keypair.create_from_uri("//Alice")


class TestBuildUploadPayload:
    def test_payload_is_hotkey_colon_sha256_utf8_bytes(self) -> None:
        payload = build_upload_payload(
            hotkey_ss58="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            sha256_hex="deadbeef" * 8,
            signature_timestamp=_TIMESTAMP,
            signature_nonce=_NONCE,
        )

        assert payload == (
            b"ditto-upload-v2:5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY:"
            + b"deadbeef" * 8
            + b":1798000000:123e4567-e89b-42d3-a456-426614174000"
        )

    def test_payload_changes_with_nonce_and_time(self) -> None:
        payload = build_upload_payload(
            hotkey_ss58="5G...",
            sha256_hex="abc",
            signature_timestamp=_TIMESTAMP,
            signature_nonce=_NONCE,
        )
        assert payload.count(b":") == 4
        assert payload != build_upload_payload(
            hotkey_ss58="5G...",
            sha256_hex="abc",
            signature_timestamp=_TIMESTAMP + 1,
            signature_nonce=_NONCE,
        )


class TestSignUploadPayload:
    def test_returns_lowercase_128_hex_signature(self) -> None:
        keypair = _make_test_keypair()

        class _LiveWallet:
            hotkey = keypair

        handle = WalletHandle(
            coldkey_name="miner",
            hotkey_name="default",
            hotkey_ss58=keypair.ss58_address,
        )

        sig_hex = sign_upload_payload(
            handle=handle,
            live_wallet=_LiveWallet(),  # type: ignore[arg-type]
            sha256_hex="deadbeef" * 8,
            signature_timestamp=_TIMESTAMP,
            signature_nonce=_NONCE,
        )

        assert len(sig_hex) == 128
        assert sig_hex == sig_hex.lower()
        bytes.fromhex(sig_hex)

    def test_signature_round_trips_through_server_verifier(self) -> None:
        """End-to-end contract: server uses
        ``Keypair(ss58_address=hotkey).verify(payload, bytes.fromhex(sig))``
        to validate the upload sig. Reproduce that flow here so payload
        drift on either side is caught in unit tests, not in production."""
        keypair = _make_test_keypair()

        class _LiveWallet:
            hotkey = keypair

        handle = WalletHandle(
            coldkey_name="miner",
            hotkey_name="default",
            hotkey_ss58=keypair.ss58_address,
        )
        sha256_hex = "ab" * 32

        sig_hex = sign_upload_payload(
            handle=handle,
            live_wallet=_LiveWallet(),  # type: ignore[arg-type]
            sha256_hex=sha256_hex,
            signature_timestamp=_TIMESTAMP,
            signature_nonce=_NONCE,
        )

        # Mirror server-side verifier verbatim.
        server_payload = (
            f"ditto-upload-v2:{handle.hotkey_ss58}:{sha256_hex}:{_TIMESTAMP}:{_NONCE}"
        ).encode("ascii")
        server_keypair = bittensor.Keypair(ss58_address=handle.hotkey_ss58)
        assert server_keypair.verify(server_payload, bytes.fromhex(sig_hex)) is True
