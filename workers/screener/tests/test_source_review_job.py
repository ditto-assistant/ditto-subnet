"""Tests for the credential-minimal one-shot Targon source-review entrypoint."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from ditto_screener import source_review_job
from ditto_screener.l2_review import InProcessAnalyzerHarness, LayeredSourceReviewAgent
from ditto_screener.policy import SourceReviewObservation


def test_build_reviewer_runs_l1_l2_l3_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / "source-review-key"
    key_path.write_text("provider-key\n")
    monkeypatch.setenv("SCREENER_NODE_CREDENTIAL_FILE", str(tmp_path / "node.json"))
    monkeypatch.setenv("SCREENER_L2_REVIEW_MODE", "enforce")
    monkeypatch.setenv("SCREENER_L3_REVIEW_ENABLED", "true")
    reviewer = source_review_job._build_reviewer(
        key_file=str(key_path), timeout_seconds=60
    )
    assert isinstance(reviewer, LayeredSourceReviewAgent)
    assert reviewer._mode == "enforce"
    assert reviewer._l2._l3_enabled is True
    assert isinstance(reviewer._l2._harness, InProcessAnalyzerHarness)


@pytest.mark.asyncio
async def test_job_binds_source_and_posts_only_bounded_observation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    review_id = "550e8400-e29b-41d4-a716-446655440000"
    attempt_id = "650e8400-e29b-41d4-a716-446655440000"
    artifact_sha256 = "a" * 64
    key_path = tmp_path / "source-review-key"
    archive_path = tmp_path / "source.tgz"
    posted: list[dict[str, Any]] = []
    monkeypatch.setenv("DITTO_PLATFORM_URL", "https://platform.example")
    monkeypatch.setenv("DITTO_SOURCE_REVIEW_ID", review_id)
    monkeypatch.setenv("DITTO_SOURCE_REVIEW_ATTEMPT_ID", attempt_id)
    monkeypatch.setenv("DITTO_SOURCE_REVIEW_ARTIFACT_SHA256", artifact_sha256)
    monkeypatch.setenv("DITTO_SOURCE_REVIEW_JOB_TOKEN", "job-" + "x" * 48)

    async def materialize_secret() -> None:
        key_path.write_text("provider-key\n")
        key_path.chmod(0o600)
        monkeypatch.setenv("SCREENER_SOURCE_REVIEW_API_KEY_FILE", str(key_path))

    async def download_verified(_client: object, url: str, expected_sha256: str) -> str:
        assert url == "https://storage.example/source.tgz"
        assert expected_sha256 == artifact_sha256
        archive_path.write_bytes(b"bound source archive")
        return str(archive_path)

    class Response:
        def __init__(self, body: dict[str, Any]) -> None:
            self._body = body

        def json(self) -> dict[str, Any]:
            return self._body

        def raise_for_status(self) -> None:
            return None

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, url: str, **_kwargs: object) -> Response:
            assert url.endswith(f"/{review_id}/source")
            return Response(
                {
                    "artifact_sha256": artifact_sha256,
                    "source_url_b64": base64.b64encode(
                        b"https://storage.example/source.tgz"
                    ).decode(),
                }
            )

        async def post(
            self, url: str, *, json: dict[str, Any], **_kwargs: object
        ) -> Response:
            assert url.endswith(f"/{review_id}/complete")
            posted.append(json)
            return Response({"verified": True})

    class Reviewer:
        def __init__(self, **values: object) -> None:
            assert values["key_file"] == str(key_path)

        async def review(self, path: str, **values: object) -> SourceReviewObservation:
            assert path == str(archive_path)
            assert values["artifact_sha256"] == artifact_sha256
            assert values["attempt_id"] == UUID(attempt_id)
            return SourceReviewObservation(
                ok=True,
                risk_level="low",
                finding_digest=None,
                categories=(),
                clearance_certified=True,
            )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        source_review_job, "_materialize_source_review_secret", materialize_secret
    )
    monkeypatch.setattr(source_review_job, "_download_verified", download_verified)
    monkeypatch.setattr(
        source_review_job.httpx, "AsyncClient", lambda **_kwargs: Client()
    )
    monkeypatch.setattr(
        source_review_job, "_build_reviewer", lambda **kwargs: Reviewer(**kwargs)
    )
    monkeypatch.setattr(source_review_job.asyncio, "sleep", no_sleep)

    assert await source_review_job._amain() == 0
    assert posted == [
        {
            "observation": {
                "ok": True,
                "risk_level": "low",
                "finding_digest": None,
                "categories": [],
                "error_code": None,
                "finding": None,
                "failure_disposition": "retryable_infra",
                "clearance_certified": True,
                "review_audit": None,
                "notes": [],
            }
        }
    ]
    assert "DITTO_SOURCE_REVIEW_JOB_TOKEN" not in source_review_job.os.environ
    assert not key_path.exists()
    assert not archive_path.exists()
