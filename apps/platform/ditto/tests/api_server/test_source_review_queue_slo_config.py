"""Shipped source-review SLO thresholds and their env overrides (SN-499)."""

from __future__ import annotations

import pytest

from ditto.api_server.errors import ApiServerConfigError
from ditto.api_server.source_review_queue_slo_config import (
    DEFAULT_SOURCE_REVIEW_MAX_AGE_SECONDS,
    DEFAULT_SOURCE_REVIEW_P95_AGE_SECONDS,
    SOURCE_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV,
    SOURCE_REVIEW_QUEUE_P95_AGE_THRESHOLD_ENV,
    parse_source_review_queue_slo_config_from_env,
)


class TestShippedThresholds:
    """SN-499: the env parser ships defaults so overdue items are visible."""

    def test_defaults_apply_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SOURCE_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV, raising=False)
        monkeypatch.delenv(SOURCE_REVIEW_QUEUE_P95_AGE_THRESHOLD_ENV, raising=False)
        config = parse_source_review_queue_slo_config_from_env()
        assert config.max_actionable_age_threshold_seconds == (
            DEFAULT_SOURCE_REVIEW_MAX_AGE_SECONDS
        )
        assert config.p95_age_threshold_seconds == DEFAULT_SOURCE_REVIEW_P95_AGE_SECONDS

    def test_env_overrides_and_off_disables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SOURCE_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV, "7200")
        monkeypatch.setenv(SOURCE_REVIEW_QUEUE_P95_AGE_THRESHOLD_ENV, "off")
        config = parse_source_review_queue_slo_config_from_env()
        assert config.max_actionable_age_threshold_seconds == 7200
        assert config.p95_age_threshold_seconds is None

    @pytest.mark.parametrize("raw", ["0", "-5", "soon"])
    def test_rejects_unusable_threshold_at_boot(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(SOURCE_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV, raw)
        with pytest.raises(ApiServerConfigError):
            parse_source_review_queue_slo_config_from_env()
