"""Unit tests for :mod:`ditto.api_server.storage.factory`."""

from __future__ import annotations

import pytest

from ditto.api_server.storage import (
    S3StorageClient,
    StorageConfig,
    create_storage_client,
)


def _make_config(**overrides: object) -> StorageConfig:
    defaults: dict[str, object] = {
        "endpoint_url": "http://minio:9000",
        "bucket": "ditto-agents",
        "access_key": "minio",
        "secret_key": "miniominio",
        "region": "us-east-1",
        "use_tls": False,
    }
    defaults.update(overrides)
    return StorageConfig(**defaults)  # type: ignore[arg-type]


class TestCreateStorageClient:
    def test_returns_s3_storage_client_with_explicit_config(self):
        config = _make_config()
        client = create_storage_client(config)
        assert isinstance(client, S3StorageClient)

    def test_reads_env_when_config_none(self, monkeypatch: pytest.MonkeyPatch):
        for key in (
            "STORAGE_ENDPOINT_URL",
            "STORAGE_BUCKET",
            "STORAGE_ACCESS_KEY",
            "STORAGE_SECRET_KEY",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("STORAGE_ENDPOINT_URL", "http://minio:9000")
        monkeypatch.setenv("STORAGE_BUCKET", "ditto-agents")
        monkeypatch.setenv("STORAGE_ACCESS_KEY", "minio")
        monkeypatch.setenv("STORAGE_SECRET_KEY", "miniominio")

        client = create_storage_client()

        assert isinstance(client, S3StorageClient)
