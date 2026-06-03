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
        monkeypatch.setenv("STORAGE_ENDPOINT_URL", "https://s3.example.com")
        monkeypatch.setenv("STORAGE_BUCKET", "custom-bucket")
        monkeypatch.setenv("STORAGE_ACCESS_KEY", "the-key")
        monkeypatch.setenv("STORAGE_SECRET_KEY", "the-secret")

        client = create_storage_client()

        # Pin the env values actually populated the config so a future
        # regression that hardcodes defaults inside the factory fails
        # rather than silently passing the isinstance assertion.
        assert isinstance(client, S3StorageClient)
        assert client._config.endpoint_url == "https://s3.example.com"
        assert client._config.bucket == "custom-bucket"
        assert client._config.access_key == "the-key"
        assert client._config.secret_key == "the-secret"
