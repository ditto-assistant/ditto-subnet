"""Promotion helpers must not leak registry tokens into errors."""

from __future__ import annotations

import subprocess

import pytest

from ditto.api_server.targon_promote import (
    TargonPromoteError,
    _skopeo_detail,
    inspect_registry_config_digest,
    mint_access_token,
)


def test_skopeo_detail_redacts_oauth_and_jwt() -> None:
    error = subprocess.CalledProcessError(
        1,
        ["skopeo", "copy"],
        "ok",
        "denied ya29.abcdefghijklmnopqrstuvwxyz0123456789 "
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig",
    )
    detail = _skopeo_detail(error)
    assert "ya29." not in detail
    assert "eyJ" not in detail
    assert "[oauth]" in detail
    assert "[jwt]" in detail


def test_mint_access_token_does_not_include_gcloud_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(
            1,
            ["gcloud", "auth", "print-access-token"],
            "token-ya29.abcdefghijklmnopqrstuvwxyz0123456789",
            "impersonation denied",
        )

    monkeypatch.setattr("ditto.api_server.targon_promote.subprocess.run", _fail)
    with pytest.raises(
        TargonPromoteError, match="registry token mint failed"
    ) as caught:
        mint_access_token("push@example.iam.gserviceaccount.com")
    assert "ya29." not in str(caught.value)
    assert caught.value.__cause__ is not None


def test_inspect_registry_config_digest_reads_manifest_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_digest = "sha256:" + "ab" * 32
    image_ref = (
        "us-central1-docker.pkg.dev/ditto-app-dev/"
        "ditto-screening-candidates/miner@sha256:" + "cd" * 32
    )

    def _inspect(args, **_kwargs):
        assert args[1] == "inspect"
        assert "--raw" in args
        assert args[-1] == f"docker://{image_ref}"
        return subprocess.CompletedProcess(
            args,
            0,
            f'{{"schemaVersion":2,"config":{{"digest":"{config_digest}"}}}}',
            "",
        )

    monkeypatch.setattr("ditto.api_server.targon_promote._run_skopeo", _inspect)
    assert inspect_registry_config_digest(image_ref, "token-" + "x" * 120) == (
        config_digest
    )


def test_inspect_registry_config_digest_rejects_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_ref = (
        "us-central1-docker.pkg.dev/ditto-app-dev/"
        "ditto-screening-candidates/miner@sha256:" + "cd" * 32
    )

    def _inspect(args, **_kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            '{"mediaType":"application/vnd.oci.image.index.v1+json","manifests":[]}',
            "",
        )

    monkeypatch.setattr("ditto.api_server.targon_promote._run_skopeo", _inspect)
    with pytest.raises(TargonPromoteError, match="index"):
        inspect_registry_config_digest(image_ref, "token-" + "x" * 120)
