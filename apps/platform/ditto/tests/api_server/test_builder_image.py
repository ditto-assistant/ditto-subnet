from __future__ import annotations

from subprocess import CalledProcessError, CompletedProcess

import pytest

from ditto.api_server.builder_image import resolve_submission_builder_image

_REPO = (
    "us-central1-docker.pkg.dev/ditto-app-dev/ditto-public-builders/submission-builder"
)
_DIGEST = "sha256:" + "ab" * 32
_LATEST = "sha256:" + "cd" * 32


def test_resolve_keeps_digest_reference() -> None:
    image = f"{_REPO}@{_DIGEST}"
    assert resolve_submission_builder_image(image) == image


def test_resolve_uses_requested_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args, **_kwargs):
        assert args[5] == f"{_REPO}:sha-deadbeef"
        return CompletedProcess(args, 0, stdout=f"{_DIGEST}\n", stderr="")

    monkeypatch.setattr("ditto.api_server.builder_image.subprocess.run", fake_run)
    assert (
        resolve_submission_builder_image(f"{_REPO}:sha-deadbeef")
        == f"{_REPO}@{_DIGEST}"
    )


def test_resolve_falls_back_to_latest_published_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(args, **_kwargs):
        if "describe" in args:
            raise CalledProcessError(1, args)
        assert "list" in args
        return CompletedProcess(args, 0, stdout=f"{_LATEST}\n", stderr="")

    monkeypatch.setattr("ditto.api_server.builder_image.subprocess.run", fake_run)
    assert (
        resolve_submission_builder_image(f"{_REPO}:sha-missing")
        == f"{_REPO}@{_LATEST}"
    )


def test_resolve_keeps_tag_when_registry_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(_args, **_kwargs):
        raise OSError("gcloud missing")

    monkeypatch.setattr("ditto.api_server.builder_image.subprocess.run", fake_run)
    image = f"{_REPO}:sha-missing"
    assert resolve_submission_builder_image(image) == image
