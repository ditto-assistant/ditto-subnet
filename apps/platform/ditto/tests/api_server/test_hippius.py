"""Config parsing for the optional Hippius avatar store."""

from __future__ import annotations

import pytest

from ditto.api_server.hippius import (
    _should_retry,
    ensure_https,
    normalize_object_key,
    parse_hippius_config_from_env,
)
from ditto.api_server.storage.errors import StorageConfigurationError


def test_missing_env_disables_hippius(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HIPPIUS_BUCKET", raising=False)
    monkeypatch.delenv("HIPPIUS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("HIPPIUS_SECRET_ACCESS_KEY", raising=False)
    assert parse_hippius_config_from_env() is None


def test_partial_env_disables_hippius(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIPPIUS_BUCKET", "avatars")
    monkeypatch.setenv("HIPPIUS_ACCESS_KEY_ID", "hip_abc")
    monkeypatch.delenv("HIPPIUS_SECRET_ACCESS_KEY", raising=False)
    assert parse_hippius_config_from_env() is None


def test_key_must_start_with_hip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIPPIUS_BUCKET", "avatars")
    monkeypatch.setenv("HIPPIUS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("HIPPIUS_SECRET_ACCESS_KEY", "secret")
    with pytest.raises(StorageConfigurationError, match="hip_"):
        parse_hippius_config_from_env()


def test_full_env_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIPPIUS_BUCKET", "avatars")
    monkeypatch.setenv("HIPPIUS_ACCESS_KEY_ID", "hip_abc")
    monkeypatch.setenv("HIPPIUS_SECRET_ACCESS_KEY", "secret")
    config = parse_hippius_config_from_env()
    assert config is not None
    assert config.bucket == "avatars"
    assert config.endpoint_url == "https://s3.hippius.com"
    assert config.region == "decentralized"


def test_http_endpoint_is_upgraded_to_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIPPIUS_BUCKET", "ditto-subnet")
    monkeypatch.setenv("HIPPIUS_ACCESS_KEY_ID", "hip_abc")
    monkeypatch.setenv("HIPPIUS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("HIPPIUS_ENDPOINT", "http://us-east-1.hippius.com")
    config = parse_hippius_config_from_env()
    assert config is not None
    assert config.endpoint_url == "https://us-east-1.hippius.com"


def test_normalize_object_key_strips_traversal() -> None:
    assert normalize_object_key("/avatars/../x.png") == "avatars/x.png"
    assert normalize_object_key("avatars//me.png") == "avatars/me.png"


def test_ensure_https() -> None:
    assert ensure_https("s3.hippius.com") == "https://s3.hippius.com"
    assert ensure_https("https://s3.hippius.com/") == "https://s3.hippius.com"


def test_retries_transient_billing_and_throttle() -> None:
    assert _should_retry(429, "")
    assert _should_retry(503, "")
    assert _should_retry(402, "UploadNotPermitted: failed to fetch billing balance")
    assert not _should_retry(402, "insufficient credit")
    assert not _should_retry(403, "SignatureDoesNotMatch")
