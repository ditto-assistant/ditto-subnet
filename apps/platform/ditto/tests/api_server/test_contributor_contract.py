"""Pinned signing formats and fail-fast OAuth configuration constraints."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from ditto.api_models.contributor_identity import cla_message, github_link_message
from ditto.api_models.http_errors import HttpErrorEnvelope
from ditto.api_server.config import (
    ApiServerConfigError,
    check_github_link_config,
    parse_github_link_config_from_env,
)
from ditto.api_server.github_link import GithubLinkConfig


def test_signing_bytes_are_pinned_to_public_protocol():
    nonce = UUID("11111111-1111-4111-8111-111111111111")
    issued = datetime(2026, 10, 13, tzinfo=UTC)
    assert (
        cla_message(
            netuid=118,
            cla_version=1,
            cla_sha256="ab" * 32,
            hotkey="hotkey",
            nonce=nonce,
            issued_at=issued,
        )
        == (
            "ditto-cla:v1:118:1:"
            + "ab" * 32
            + ":hotkey:11111111-1111-4111-8111-111111111111:"
            "2026-10-13T00:00:00.000000+00:00"
        ).encode()
    )
    assert github_link_message(
        netuid=118,
        hotkey="hotkey",
        github_user_id=100,
        github_login="miner",
        link_attempt_id=nonce,
        nonce=nonce,
        issued_at=issued,
        key_kind="coldkey",
        signer="owner",
    ) == (
        b"ditto-github-link:v1:118:hotkey:100:miner:11111111-1111-4111-8111-111111111111:11111111-1111-4111-8111-111111111111:2026-10-13T00:00:00.000000+00:00:coldkey:owner"
    )


def test_expected_not_found_responses_use_the_actual_error_envelope(app):
    schema = app.openapi()
    paths = schema["paths"]
    for path, method in (
        ("/api/v1/public/cla/versions/{version}", "get"),
        ("/api/v1/public/cla/versions/{version}/text", "get"),
        ("/api/v1/me/github-link/attempts/{attempt_id}", "get"),
        ("/api/v1/me/github-link/confirm", "post"),
    ):
        content = paths[path][method]["responses"]["404"]["content"]
        assert set(content) == {"application/json"}
        assert (
            content["application/json"]["schema"]["$ref"]
            == "#/components/schemas/HttpErrorEnvelope"
        )
    text_operation = paths["/api/v1/public/cla/versions/{version}/text"]["get"]
    assert "text/plain" in text_operation["responses"]["200"]["content"]
    assert "content" not in text_operation["responses"]["304"]
    assert any(
        parameter["name"] == "If-None-Match" and parameter["in"] == "header"
        for parameter in text_operation["parameters"]
    )
    for path, statuses in (
        ("/api/v1/me/github-link/start", (401, 403, 429, 503)),
        ("/api/v1/me/github-link/confirm", (400, 401, 403, 404, 409, 503)),
        ("/api/v1/me/github-link/unlink", (400, 401, 403, 409, 503)),
        ("/api/v1/cla/sign", (400, 409, 503)),
    ):
        for status in statuses:
            error_schema = paths[path]["post"]["responses"][str(status)]["content"][
                "application/json"
            ]["schema"]
            assert error_schema["$ref"] == "#/components/schemas/HttpErrorEnvelope"
    assert (
        HttpErrorEnvelope.model_validate(
            {
                "error_code": 3002,
                "message": "fixture",
                "request_id": None,
                "future": "ignored",
            }
        ).message
        == "fixture"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost.evil.example/api/v1/miner-auth/github/callback",
        "http://example.com/api/v1/miner-auth/github/callback",
        "https://user:password@example.com/api/v1/miner-auth/github/callback",
        "https://example.com/api/v1/miner-auth/github/callback?state=controlled",
        "https://example.com/api/v1/miner-auth/github/callback#fragment",
        "https://example.com/wrong-callback",
        "https://example.com:abc/api/v1/miner-auth/github/callback",
        "https://example.com:65536/api/v1/miner-auth/github/callback",
        "http://localhost:-1/api/v1/miner-auth/github/callback",
    ],
)
def test_oauth_config_rejects_unsafe_redirects(url):
    with pytest.raises(ApiServerConfigError):
        check_github_link_config(
            GithubLinkConfig(
                enabled=True,
                client_id="fixture",
                client_secret="secret",
                redirect_url=url,
            )
        )


def test_oauth_defaults_off_and_requires_secret_when_enabled(monkeypatch):
    for name in (
        "GITHUB_LINK_ENABLED",
        "GITHUB_LINK_CLIENT_ID",
        "GITHUB_LINK_CLIENT_SECRET",
        "GITHUB_LINK_REDIRECT_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    config = parse_github_link_config_from_env()
    assert config.enabled is False
    check_github_link_config(config)
    with pytest.raises(ApiServerConfigError):
        check_github_link_config(replace(config, enabled=True))
    check_github_link_config(
        GithubLinkConfig(
            enabled=True,
            client_id="fixture",
            client_secret="secret",
            redirect_url="http://localhost/api/v1/miner-auth/github/callback",
        )
    )
