"""Unit tests for the code embedder (:mod:`ditto.api_server.embedding.config`)."""

from __future__ import annotations

import pytest

from ditto.api_server.embedding import (
    EmbeddingConfig,
    EmbeddingConfigError,
    parse_embedding_config_from_env,
)
from ditto.api_server.embedding.config import check_embedding_config


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "CODE_EMBEDDER_URL",
        "CODE_EMBEDDER_MODEL",
        "CODE_EMBEDDER_REVISION",
        "CODE_EMBEDDER_DIM",
        "CODE_EMBEDDER_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)


class TestParse:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        config = parse_embedding_config_from_env()
        assert config.enabled is False
        assert config.url is None
        assert config.timeout_seconds == 5.0

    def test_enabled_when_url_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("CODE_EMBEDDER_URL", "http://embedder:80")
        monkeypatch.setenv("CODE_EMBEDDER_MODEL", "Qwen/Qwen3-Embedding-0.6B")
        monkeypatch.setenv("CODE_EMBEDDER_DIM", "256")
        config = parse_embedding_config_from_env()
        assert config.enabled is True
        assert config.dim == 256
        assert config.revision == "main"  # default
        assert config.model_tag == "Qwen/Qwen3-Embedding-0.6B@main"

    def test_enabled_requires_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("CODE_EMBEDDER_URL", "http://embedder:80")
        with pytest.raises(EmbeddingConfigError, match="CODE_EMBEDDER_MODEL"):
            parse_embedding_config_from_env()

    def test_bad_dim_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("CODE_EMBEDDER_DIM", "notanint")
        with pytest.raises(EmbeddingConfigError, match="CODE_EMBEDDER_DIM"):
            parse_embedding_config_from_env()


class TestCheck:
    def _cfg(self, **kw: object) -> EmbeddingConfig:
        base: dict[str, object] = {
            "url": None,
            "model": "",
            "revision": "main",
            "dim": None,
            "timeout_seconds": 5.0,
            "auth": "none",
        }
        base.update(kw)
        return EmbeddingConfig(**base)  # type: ignore[arg-type]

    def test_disabled_config_is_valid(self) -> None:
        check_embedding_config(self._cfg())  # no raise

    def test_nonpositive_timeout_rejected(self) -> None:
        with pytest.raises(EmbeddingConfigError, match="TIMEOUT"):
            check_embedding_config(self._cfg(timeout_seconds=0.0))

    def test_nonpositive_dim_rejected(self) -> None:
        with pytest.raises(EmbeddingConfigError, match="DIM"):
            check_embedding_config(self._cfg(dim=0))

    def test_enabled_non_http_url_rejected(self) -> None:
        with pytest.raises(EmbeddingConfigError, match="http"):
            check_embedding_config(
                self._cfg(url="embedder:80", model="m")  # missing scheme
            )


class TestAuth:
    def test_auth_defaults_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        assert parse_embedding_config_from_env().auth == "none"

    def test_auth_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("CODE_EMBEDDER_AUTH", "gcp_id_token")
        assert parse_embedding_config_from_env().auth == "gcp_id_token"

    def test_invalid_auth_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("CODE_EMBEDDER_AUTH", "basic")
        with pytest.raises(EmbeddingConfigError, match="CODE_EMBEDDER_AUTH"):
            parse_embedding_config_from_env()
