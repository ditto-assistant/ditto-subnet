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

from types import SimpleNamespace

import bittensor

from ditto.miner_cli.models import WalletHandle
from ditto.miner_cli.signing import (
    build_harness_logs_payload,
    build_upload_payload,
    sign_harness_logs_request,
    sign_upload_payload,
)


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


class TestBuildHarnessLogsPayload:
    """The signed bytes behind ``ditto logs``.

    The CLI and the platform each hold their own copy of this builder -- the
    CLI ships independently and must not depend on the platform -- so the two
    are only correct together. These tests are what keeps them that way.
    """

    def test_payload_is_the_versioned_four_field_form(self) -> None:
        payload = build_harness_logs_payload(
            hotkey_ss58="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            agent_id="5fdadd33-bd0f-492d-ba71-49bef159f069",
            requested_at="2026-08-14T13:27:36.760189+00:00",
        )

        assert payload == (
            b"ditto-harness-logs:v1:"
            b"5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY:"
            b"5fdadd33-bd0f-492d-ba71-49bef159f069:"
            b"2026-08-14T13:27:36.760189+00:00"
        )

    def test_agent_id_is_inside_the_signed_bytes(self) -> None:
        """Two agents, same hotkey and instant, must not share a signature.

        Signing only the hotkey would let one captured signature be re-pointed
        at any agent the miner owns. Binding the pair means a signature
        authorizes exactly one lookup.
        """
        common = {
            "hotkey_ss58": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            "requested_at": "2026-08-14T13:27:36.760189+00:00",
        }

        assert build_harness_logs_payload(
            agent_id="5fdadd33-bd0f-492d-ba71-49bef159f069", **common
        ) != build_harness_logs_payload(
            agent_id="c7169eb6-ae40-4a90-9e90-d3da579f3c39", **common
        )

    def test_requested_at_is_inside_the_signed_bytes(self) -> None:
        """A captured signature must not stay valid for a later instant.

        This is what the freshness window rests on: bind the timestamp, and an
        attacker replaying an old signature cannot advance it into the window.
        """
        common = {
            "hotkey_ss58": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            "agent_id": "5fdadd33-bd0f-492d-ba71-49bef159f069",
        }

        assert build_harness_logs_payload(
            requested_at="2026-08-14T13:27:36.760189+00:00", **common
        ) != build_harness_logs_payload(
            requested_at="2026-08-14T13:27:36.760190+00:00", **common
        )

    def test_cli_signature_verifies_the_way_the_platform_verifies_it(self) -> None:
        """End-to-end signature contract against the real bittensor library.

        Mirrors the platform's ``_verify_signature``: rebuild the payload from
        the same fields, then ``Keypair.verify`` from the SS58 address alone --
        which is all the server has.
        """
        keypair = _make_test_keypair()
        handle = WalletHandle(
            coldkey_name="test", hotkey_name="test", hotkey_ss58=keypair.ss58_address
        )
        agent_id = "5fdadd33-bd0f-492d-ba71-49bef159f069"
        requested_at = "2026-08-14T13:27:36.760189+00:00"

        signature = sign_harness_logs_request(
            handle=handle,
            live_wallet=SimpleNamespace(hotkey=keypair),
            agent_id=agent_id,
            requested_at=requested_at,
        )

        server_side = bittensor.Keypair(ss58_address=keypair.ss58_address)
        assert server_side.verify(
            build_harness_logs_payload(
                hotkey_ss58=handle.hotkey_ss58,
                agent_id=agent_id,
                requested_at=requested_at,
            ),
            bytes.fromhex(signature),
        )

    def test_another_hotkeys_signature_does_not_verify(self) -> None:
        """The whole authorization rests on this failing."""
        alice = _make_test_keypair()
        bob = bittensor.Keypair.create_from_uri("//Bob")
        agent_id = "5fdadd33-bd0f-492d-ba71-49bef159f069"
        requested_at = "2026-08-14T13:27:36.760189+00:00"

        forged = sign_harness_logs_request(
            handle=WalletHandle(
                coldkey_name="t", hotkey_name="t", hotkey_ss58=alice.ss58_address
            ),
            live_wallet=SimpleNamespace(hotkey=bob),
            agent_id=agent_id,
            requested_at=requested_at,
        )

        assert not bittensor.Keypair(ss58_address=alice.ss58_address).verify(
            build_harness_logs_payload(
                hotkey_ss58=alice.ss58_address,
                agent_id=agent_id,
                requested_at=requested_at,
            ),
            bytes.fromhex(forged),
        )
