"""Health contract for long-lived workers and one-shot source review jobs."""

from __future__ import annotations

import pytest

from ditto_screener import health


@pytest.mark.asyncio
async def test_source_review_job_skips_long_lived_worker_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DITTO_SOURCE_REVIEW_JOB", "1")

    def unexpected_config_parse() -> None:
        raise AssertionError("source review health must not parse worker config")

    monkeypatch.setattr(
        health, "parse_screener_config_from_env", unexpected_config_parse
    )

    await health._check()
