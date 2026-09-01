from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from ditto.api_server.coding_storage_data_plane_canary import (
    CodingStorageCanaryError,
    CuratorSeedConfig,
    PlatformVerifyConfig,
    _assert_curator_secret_denied,
    _metadata_identity_and_token,
    _read_owner_only_secret_file,
    _read_platform_secret,
    _write_receipt,
    seed_private_input,
    verify_platform,
)

_SOURCE_SHA = "ab" * 20


class _Backend:
    def __init__(self) -> None:
        self.seeded: tuple[str, bytes] | None = None
        self.verified: tuple[str, bytes, str, bytes] | None = None

    async def seed_private_input(
        self, config: CuratorSeedConfig, *, key: str, payload: bytes
    ) -> dict[str, object]:
        del config
        self.seeded = (key, payload)
        return {
            "curator_create_succeeded": True,
            "private_input_overwrite_attempted": False,
            "object_delete_attempted": False,
        }

    async def verify_platform(
        self,
        config: PlatformVerifyConfig,
        *,
        private_key: str,
        private_payload: bytes,
        evidence_key: str,
        evidence_payload: bytes,
    ) -> dict[str, object]:
        del config
        self.verified = (
            private_key,
            private_payload,
            evidence_key,
            evidence_payload,
        )
        return {
            "platform_identity": (
                "ditto-platform-api@ditto-app-dev.iam.gserviceaccount.com"
            ),
            "curator_secret_denied": True,
            "private_input_exact_get": True,
            "reader_list_denied": True,
            "reader_put_denied": True,
            "reader_delete_denied": True,
            "reader_cross_authority_denied": True,
            "evidence_created": True,
            "evidence_head_verified": True,
            "evidence_full_sha256_verified": True,
            "finalizer_list_denied": True,
            "finalizer_delete_denied": True,
            "finalizer_cross_authority_denied": True,
        }


async def test_separate_modes_emit_redacted_content_addressed_receipts(
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "curator.secret"
    secret_file.write_text("curator-secret")
    secret_file.chmod(0o600)
    backend = _Backend()

    seed_receipt = await seed_private_input(
        CuratorSeedConfig(
            project="ditto-app-dev",
            environment="prod",
            source_sha=_SOURCE_SHA,
            curator_access_key="curator-access",
            curator_secret_file=secret_file,
            confirmation="SEED CODING STORAGE PRIVATE INPUT CANARY",
        ),
        backend=backend,
    )
    verify_receipt = await verify_platform(
        PlatformVerifyConfig(
            project="ditto-app-dev",
            environment="prod",
            source_sha=_SOURCE_SHA,
            private_input_access_key="reader-access",
            evidence_access_key="finalizer-access",
            confirmation="RUN CODING STORAGE DATA PLANE CANARY",
        ),
        backend=backend,
    )

    assert backend.seeded is not None
    assert backend.verified is not None
    assert backend.seeded[0] == backend.verified[0]
    assert backend.seeded[1] == backend.verified[1]
    assert verify_receipt["sealed_evidence"]["key"].startswith(
        "coding-evidence/v1/terminal-publication-acknowledgement/sha256/"
    )
    rendered = json.dumps([seed_receipt, verify_receipt], sort_keys=True)
    for secret in (
        "curator-secret",
        "curator-access",
        "reader-access",
        "finalizer-access",
        "https://",
    ):
        assert secret not in rendered
    assert len(seed_receipt["receipt_sha256"]) == 64
    assert len(verify_receipt["receipt_sha256"]) == 64


def test_confirmation_and_authority_separation_fail_closed(tmp_path: Path) -> None:
    secret_file = tmp_path / "curator.secret"
    secret_file.write_text("secret")
    secret_file.chmod(0o600)
    with pytest.raises(CodingStorageCanaryError, match="confirmation"):
        CuratorSeedConfig(
            project="ditto-app-dev",
            environment="prod",
            source_sha=_SOURCE_SHA,
            curator_access_key="curator-access",
            curator_secret_file=secret_file,
            confirmation="wrong",
        )
    with pytest.raises(CodingStorageCanaryError, match="must differ"):
        PlatformVerifyConfig(
            project="ditto-app-dev",
            environment="prod",
            source_sha=_SOURCE_SHA,
            private_input_access_key="shared-access",
            evidence_access_key="shared-access",
            confirmation="RUN CODING STORAGE DATA PLANE CANARY",
        )


def test_curator_secret_file_must_be_owner_only(tmp_path: Path) -> None:
    secret_file = tmp_path / "curator.secret"
    secret_file.write_text("secret")
    secret_file.chmod(0o644)
    with pytest.raises(CodingStorageCanaryError, match="mode 0600"):
        _read_owner_only_secret_file(secret_file)
    secret_file.chmod(0o600)
    assert _read_owner_only_secret_file(secret_file) == "secret"


def test_receipt_is_exclusive_mode_0600(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    receipt = {"receipt_sha256": "cd" * 32, "secret_values_persisted": False}
    _write_receipt(receipt, output)
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text()) == receipt
    with pytest.raises(CodingStorageCanaryError, match="new writable file"):
        _write_receipt(receipt, output)


async def test_platform_secret_reads_are_identity_bound_and_in_memory() -> None:
    expected_email = "ditto-platform-api@ditto-app-dev.iam.gserviceaccount.com"
    observed_authorization: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "metadata.google.internal":
            assert request.headers.get("Metadata-Flavor") == "Google"
            if request.url.path.endswith("/email"):
                return httpx.Response(200, text=expected_email)
            return httpx.Response(200, json={"access_token": "metadata-token"})
        observed_authorization.append(request.headers.get("Authorization", ""))
        if "coding-input-curator-prod-hmac-secret" in request.url.path:
            return httpx.Response(403)
        return httpx.Response(
            200,
            json={"payload": {"data": base64.b64encode(b"reader-secret").decode()}},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as client:
        identity, token = await _metadata_identity_and_token(client, "ditto-app-dev")
        config = PlatformVerifyConfig(
            project="ditto-app-dev",
            environment="prod",
            source_sha=_SOURCE_SHA,
            private_input_access_key="reader-access",
            evidence_access_key="finalizer-access",
            confirmation="RUN CODING STORAGE DATA PLANE CANARY",
        )
        await _assert_curator_secret_denied(client, token, config)
        secret = await _read_platform_secret(
            client,
            token,
            project="ditto-app-dev",
            secret="coding-input-reader-prod-hmac-secret",
        )

    assert identity == expected_email
    assert secret == "reader-secret"
    assert observed_authorization == ["Bearer metadata-token", "Bearer metadata-token"]


async def test_platform_mode_rejects_wrong_attached_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/email"):
            return httpx.Response(200, text="default@developer.gserviceaccount.com")
        return httpx.Response(200, json={"access_token": "metadata-token"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as client:
        with pytest.raises(CodingStorageCanaryError, match="not ditto-platform-api"):
            await _metadata_identity_and_token(client, "ditto-app-dev")
