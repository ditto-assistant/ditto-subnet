from __future__ import annotations

import asyncio
import hashlib
import json
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ditto.api_server.coding_hippius_probe import (
    HippiusProbeConfig,
    HippiusProbeConfigurationError,
    HippiusProbeCredential,
    HippiusProbeReceiptError,
    HippiusProbeTransportError,
)
from ditto.api_server.coding_hippius_revocation import (
    HIPPIUS_MANAGEMENT_API_ORIGIN,
    HIPPIUS_REVOCATION_RECEIPT_SCHEMA,
    HippiusRevocationManagementConfig,
    HippiusRevocationTarget,
    run_hippius_revocation_observation,
    write_hippius_revocation_receipt,
)


def _credential(name: str) -> HippiusProbeCredential:
    return HippiusProbeCredential(f"hip_{name}_access", f"{name}-value")


def _config() -> HippiusProbeConfig:
    return HippiusProbeConfig(
        endpoint_url="https://s3.hippius.com",
        private_input_bucket="coding-private-inputs",
        sealed_evidence_bucket="coding-sealed-evidence",
        private_input_curator=_credential("curator"),
        private_input_reader=_credential("reader"),
        evidence_mediator=_credential("evidence"),
        timeout_seconds=5.0,
    )


def _target() -> HippiusRevocationTarget:
    return HippiusRevocationTarget(
        "1814e363-0e24-4e22-855a-425b4dc43f94", _credential("temporary")
    )


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.revoked = False
        self.deleted: list[tuple[str, str]] = []

    async def put_synthetic(self, *, bucket: str, key: str, body: bytes) -> None:
        self.objects[(bucket, key)] = body

    async def get_as_target(
        self, *, target: HippiusRevocationTarget, bucket: str, key: str
    ) -> bytes:
        del target
        if self.revoked:
            from ditto.api_server.coding_hippius_probe import HippiusProbeAccessDenied

            raise HippiusProbeAccessDenied("denied")
        return self.objects[(bucket, key)]

    async def delete_synthetic(self, *, bucket: str, key: str) -> None:
        self.deleted.append((bucket, key))
        self.objects.pop((bucket, key), None)


class _Management:
    def __init__(self, storage: _Storage) -> None:
        self.storage = storage
        self.revoked: list[str] = []

    async def revoke_sub_token(self, *, token_id: str) -> None:
        self.revoked.append(token_id)
        self.storage.revoked = True


def _clock() -> Callable[[], datetime]:
    instant = datetime(2026, 9, 3, tzinfo=UTC)

    def now() -> datetime:
        nonlocal instant
        value = instant
        instant += timedelta(milliseconds=100)
        return value

    return now


def _entropy() -> Callable[[int], bytes]:
    def generate(size: int) -> bytes:
        return b"x" * size

    return generate


def _monotonic() -> Callable[[], float]:
    instant = 10.0

    def tick() -> float:
        nonlocal instant
        value = instant
        instant += 0.1
        return value

    return tick


def test_revocation_observation_records_first_denial_and_cleans_up() -> None:
    storage = _Storage()
    management = _Management(storage)

    receipt = asyncio.run(
        run_hippius_revocation_observation(
            config=_config(),
            target=_target(),
            storage=storage,
            management=management,
            source_sha="a" * 40,
            provider_profile_payload_sha256="b" * 64,
            now=_clock(),
            synthetic_bytes=_entropy(),
            monotonic=_monotonic(),
        )
    )

    assert receipt.schema == HIPPIUS_REVOCATION_RECEIPT_SCHEMA
    assert receipt.rejection_delay_milliseconds == 100
    assert receipt.post_revoke_attempts == 1
    assert management.revoked == [_target().token_id]
    assert storage.deleted and not storage.objects
    assert (
        receipt.target_token_sha256
        == hashlib.sha256(_target().token_id.encode("ascii")).hexdigest()
    )


def test_revocation_observation_rejects_a_production_identity() -> None:
    with pytest.raises(HippiusProbeConfigurationError, match="unsafe"):
        asyncio.run(
            run_hippius_revocation_observation(
                config=_config(),
                target=HippiusRevocationTarget(
                    token_id=_target().token_id,
                    credential=_credential("reader"),
                ),
                storage=_Storage(),
                management=_Management(_Storage()),
                source_sha="a" * 40,
                provider_profile_payload_sha256="b" * 64,
            )
        )


def test_revocation_observation_fails_when_target_is_not_usable() -> None:
    storage = _Storage()
    storage.revoked = True
    with pytest.raises(HippiusProbeTransportError, match="not usable"):
        asyncio.run(
            run_hippius_revocation_observation(
                config=_config(),
                target=_target(),
                storage=storage,
                management=_Management(storage),
                source_sha="a" * 40,
                provider_profile_payload_sha256="b" * 64,
                now=_clock(),
                synthetic_bytes=_entropy(),
                monotonic=_monotonic(),
            )
        )


def test_revocation_receipt_is_redacted_and_exclusive(tmp_path: Path) -> None:
    storage = _Storage()
    receipt = asyncio.run(
        run_hippius_revocation_observation(
            config=_config(),
            target=_target(),
            storage=storage,
            management=_Management(storage),
            source_sha="a" * 40,
            provider_profile_payload_sha256="b" * 64,
            now=_clock(),
            synthetic_bytes=_entropy(),
            monotonic=_monotonic(),
        )
    )
    output = tmp_path / "receipt.json"
    digest = write_hippius_revocation_receipt(receipt=receipt, output=output)

    body = output.read_text()
    payload = json.loads(body)
    assert payload["receipt_payload_sha256"] == digest
    assert _target().credential.access_key not in body
    assert _target().credential.secret_key not in body
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(HippiusProbeReceiptError, match="safely creatable"):
        write_hippius_revocation_receipt(receipt=receipt, output=output)


def test_management_configuration_is_pinned_to_the_management_origin() -> None:
    config = HippiusRevocationManagementConfig(access_token="x")
    assert config.endpoint_url == HIPPIUS_MANAGEMENT_API_ORIGIN
    with pytest.raises(HippiusProbeConfigurationError, match="unsafe"):
        HippiusRevocationManagementConfig(
            access_token="x", endpoint_url="https://api.invalid"
        )
