"""Unit tests for the data-pipeline generate client config."""

from __future__ import annotations

import pytest

from ditto.api_server.datapipeline import (
    DataPipelineConfigError,
    parse_data_pipeline_config_from_env,
)


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "DATA_PIPELINE_URL",
        "DATA_PIPELINE_RUN_SIZE",
        "DATA_PIPELINE_AUTH",
        "DATA_PIPELINE_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = parse_data_pipeline_config_from_env()
    assert cfg.enabled is False
    assert cfg.run_size == "full"  # default profile even when disabled


def test_enabled_parses_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_PIPELINE_URL", "https://gen.example")
    monkeypatch.setenv("DATA_PIPELINE_RUN_SIZE", "medium")
    monkeypatch.setenv("DATA_PIPELINE_AUTH", "gcp_id_token")
    monkeypatch.setenv("DATA_PIPELINE_TIMEOUT_SECONDS", "45")
    cfg = parse_data_pipeline_config_from_env()
    assert cfg.enabled is True
    assert cfg.run_size == "medium"
    assert cfg.auth == "gcp_id_token"
    assert cfg.timeout_seconds == 45.0


def test_bad_run_size_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_PIPELINE_URL", "https://gen.example")
    monkeypatch.setenv("DATA_PIPELINE_RUN_SIZE", "xl")
    with pytest.raises(DataPipelineConfigError):
        parse_data_pipeline_config_from_env()


def test_non_http_url_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_PIPELINE_URL", "ftp://gen.example")
    with pytest.raises(DataPipelineConfigError):
        parse_data_pipeline_config_from_env()


def test_bad_timeout_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_PIPELINE_TIMEOUT_SECONDS", "not-a-float")
    with pytest.raises(DataPipelineConfigError):
        parse_data_pipeline_config_from_env()
