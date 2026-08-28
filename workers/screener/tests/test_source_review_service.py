from __future__ import annotations

import os

import pytest

from ditto_screener import source_review_service


def _env_rows() -> list[list[str]]:
    return [
        ["DITTO_PLATFORM_URL", "https://platform.example"],
        ["DITTO_SOURCE_REVIEW_ID", "550e8400-e29b-41d4-a716-446655440000"],
        ["DITTO_SOURCE_REVIEW_ATTEMPT_ID", "650e8400-e29b-41d4-a716-446655440000"],
        ["DITTO_SOURCE_REVIEW_ARTIFACT_SHA256", "a" * 64],
        ["DITTO_SOURCE_REVIEW_JOB_TOKEN", "job-" + "x" * 48],
        ["SCREENER_GCP_BOOTSTRAP_ACCESS_TOKEN", "bootstrap-" + "x" * 48],
        ["SCREENER_SOURCE_REVIEW_SECRET_RESOURCE", "projects/p/secrets/reviewer"],
    ]


def test_warm_service_accepts_only_the_bounded_review_environment() -> None:
    parsed = source_review_service._parse_env({"env": _env_rows()})
    assert parsed["DITTO_SOURCE_REVIEW_ARTIFACT_SHA256"] == "a" * 64

    with pytest.raises(ValueError, match="name"):
        source_review_service._parse_env(
            {"env": [*_env_rows(), ["TARGON_API_KEY", "must-not-enter"]]}
        )


def test_warm_service_restores_ephemeral_credentials_after_each_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DITTO_SOURCE_REVIEW_JOB_TOKEN", raising=False)
    monkeypatch.setenv("DITTO_PLATFORM_URL", "https://standing.example")
    values = source_review_service._parse_env({"env": _env_rows()})

    with source_review_service._temporary_environment(values):
        assert os.environ["DITTO_SOURCE_REVIEW_JOB_TOKEN"].startswith("job-")
        os.environ.pop("DITTO_SOURCE_REVIEW_JOB_TOKEN")
        assert os.environ["DITTO_PLATFORM_URL"] == "https://platform.example"

    assert "DITTO_SOURCE_REVIEW_JOB_TOKEN" not in os.environ
    assert os.environ["DITTO_PLATFORM_URL"] == "https://standing.example"
