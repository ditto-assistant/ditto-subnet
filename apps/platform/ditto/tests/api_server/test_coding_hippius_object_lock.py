from __future__ import annotations

import asyncio
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ditto.api_server.coding_hippius_object_lock import (
    HIPPIUS_OBJECT_LOCK_RECEIPT_SCHEMA,
    HippiusObjectLockCanaryConfig,
    run_hippius_object_lock_canary,
    write_hippius_object_lock_receipt,
)
from ditto.api_server.coding_hippius_probe import (
    HippiusProbeAccessDenied,
    HippiusProbeConfigurationError,
    HippiusProbeCredential,
    HippiusProbeTransportError,
)


def _config(**overrides: object) -> HippiusObjectLockCanaryConfig:
    values: dict[str, object] = {
        "endpoint_url": "https://s3.hippius.com",
        "master_credential": HippiusProbeCredential(
            "hip_object_lock_access", "object-lock-value"
        ),
    }
    values.update(overrides)
    return HippiusObjectLockCanaryConfig(**values)  # type: ignore[arg-type]


class _Transport:
    def __init__(self, *, delete_allowed: bool = False) -> None:
        self.delete_allowed = delete_allowed
        self.bucket: str | None = None
        self.versioning = False
        self.retention = False
        self.versions: dict[str, bytes] = {}
        self.current_deleted = False

    async def create_disposable_bucket(self, *, bucket: str) -> None:
        self.bucket = bucket

    async def enable_versioning(self, *, bucket: str) -> None:
        assert bucket == self.bucket
        self.versioning = True

    async def versioning_enabled(self, *, bucket: str) -> bool:
        assert bucket == self.bucket
        return self.versioning

    async def configure_compliance_retention(self, *, bucket: str) -> None:
        assert bucket == self.bucket and self.versioning
        self.retention = True

    async def compliance_retention_days(self, *, bucket: str) -> int | None:
        assert bucket == self.bucket
        return 1 if self.retention else None

    async def put_object(self, *, bucket: str, key: str, body: bytes) -> str:
        assert bucket == self.bucket and key == "canary.bin" and self.retention
        version = f"v{len(self.versions) + 1}"
        self.versions[version] = body
        return version

    async def delete_version(self, *, bucket: str, key: str, version_id: str) -> None:
        assert bucket == self.bucket and key == "canary.bin"
        if self.delete_allowed:
            self.versions.pop(version_id)
            return
        raise HippiusProbeAccessDenied("denied")

    async def get_version(self, *, bucket: str, key: str, version_id: str) -> bytes:
        assert bucket == self.bucket and key == "canary.bin"
        return self.versions[version_id]

    async def delete_current(self, *, bucket: str, key: str) -> bool:
        assert bucket == self.bucket and key == "canary.bin"
        self.current_deleted = True
        return True


def _clock() -> Callable[[], datetime]:
    return lambda: datetime(2026, 9, 3, tzinfo=UTC)


def test_object_lock_canary_requires_version_aware_retention() -> None:
    transport = _Transport()
    receipt = asyncio.run(
        run_hippius_object_lock_canary(
            config=_config(),
            transport=transport,
            source_sha="a" * 40,
            provider_profile_payload_sha256="b" * 64,
            now=_clock(),
            synthetic_bytes=lambda size: b"x" * size,
        )
    )
    assert receipt.schema == HIPPIUS_OBJECT_LOCK_RECEIPT_SCHEMA
    assert receipt.retained_disposable_bucket is True
    assert receipt.retained_locked_versions == 2
    assert transport.current_deleted is True


def test_object_lock_canary_rejects_permanent_deletion() -> None:
    with pytest.raises(HippiusProbeTransportError, match="permanent deletion"):
        asyncio.run(
            run_hippius_object_lock_canary(
                config=_config(),
                transport=_Transport(delete_allowed=True),
                source_sha="a" * 40,
                provider_profile_payload_sha256="b" * 64,
                now=_clock(),
                synthetic_bytes=lambda size: b"x" * size,
            )
        )


def test_object_lock_canary_requires_configuration_readback() -> None:
    class _UnobservableVersioning(_Transport):
        async def versioning_enabled(self, *, bucket: str) -> bool:
            assert bucket == self.bucket
            return False

    with pytest.raises(HippiusProbeTransportError, match="versioning readback"):
        asyncio.run(
            run_hippius_object_lock_canary(
                config=_config(),
                transport=_UnobservableVersioning(),
                source_sha="a" * 40,
                provider_profile_payload_sha256="b" * 64,
                now=_clock(),
                synthetic_bytes=lambda size: b"x" * size,
            )
        )


def test_object_lock_receipt_is_redacted_and_exclusive(tmp_path: Path) -> None:
    receipt = asyncio.run(
        run_hippius_object_lock_canary(
            config=_config(),
            transport=_Transport(),
            source_sha="a" * 40,
            provider_profile_payload_sha256="b" * 64,
            now=_clock(),
            synthetic_bytes=lambda size: b"x" * size,
        )
    )
    output = tmp_path / "receipt.json"
    write_hippius_object_lock_receipt(receipt=receipt, output=output)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "hip_object_lock_access" not in output.read_text()


def test_object_lock_config_rejects_non_hippius_origin() -> None:
    with pytest.raises(HippiusProbeConfigurationError, match="unsafe"):
        _config(endpoint_url="https://s3.invalid")
