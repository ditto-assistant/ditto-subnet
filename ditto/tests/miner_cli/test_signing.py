"""Unit tests for :mod:`ditto.miner_cli.signing`.

Three invariants pinned:

- The payload bytes are exactly ``f"{hotkey}:{sha256}"`` encoded UTF-8
  (matches the server's ``_verify_signature`` at
  ``ditto/api_server/endpoints/upload.py:198``).
- ``sign_upload_payload`` returns a 128-hex string matching the
  server's ``_SIGNATURE_HEX_PATTERN``.
- A CLI-produced signature round-trips through the server-side
  ``bittensor.Keypair.verify`` flow (true end-to-end sig contract check
  using the real bittensor library; no network).
"""

from __future__ import annotations

import bittensor

from ditto.miner_cli.models import WalletHandle
from ditto.miner_cli.signing import build_upload_payload, sign_upload_payload


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
        )

        assert payload == b"5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY:" + (
            b"deadbeef" * 8
        )

    def test_payload_does_not_include_a_version_field(self) -> None:
        """Regression guard: spec drift would re-add ``:{version}`` here.
        Server verifier expects exactly two colon-separated fields."""
        payload = build_upload_payload(hotkey_ss58="5G...", sha256_hex="abc")
        assert payload.count(b":") == 1


class TestSignUploadPayload:
    def test_returns_lowercase_128_hex_signature(self) -> None:
        keypair = _make_test_keypair()
        # Build a minimal "live wallet" shim exposing only what signing.py touches.

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
        )

        assert len(sig_hex) == 128
        assert sig_hex == sig_hex.lower()
        # Must decode as hex.
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
        )

        # Mirror server-side verifier verbatim.
        server_payload = f"{handle.hotkey_ss58}:{sha256_hex}".encode()
        server_keypair = bittensor.Keypair(ss58_address=handle.hotkey_ss58)
        assert server_keypair.verify(server_payload, bytes.fromhex(sig_hex)) is True
