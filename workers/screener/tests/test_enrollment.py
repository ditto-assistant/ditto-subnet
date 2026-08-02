from __future__ import annotations

import logging
import os
import stat
from base64 import b64encode
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from ditto_screener import enrollment
from ditto_screener.enrollment import (
    NodeCredential,
    load_node_credential,
    store_node_credential,
)


@pytest.fixture(autouse=True)
def _restore_screener_logger_state() -> Iterator[None]:
    """Keep bittensor's import-time logger clamp from leaking across tests."""

    prefix = "ditto_screener"
    names = {
        prefix,
        *(
            name
            for name in logging.Logger.manager.loggerDict
            if name.startswith(f"{prefix}.")
        ),
    }
    before = {
        name: (
            logging.getLogger(name).level,
            logging.getLogger(name).disabled,
            logging.getLogger(name).propagate,
        )
        for name in names
    }
    yield
    for name, (level, disabled, propagate) in before.items():
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.disabled = disabled
        logger.propagate = propagate


def _credential() -> NodeCredential:
    return NodeCredential(
        environment="test",
        node_id="ditto-screener-test",
        provider="test",
        provider_resource_id="resource-test",
        screener_hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        mnemonic=(
            "bottom drive obey lake curtain smoke basket hold race lonely fit walk"
        ),
        api_token="test-node-token-at-least-43-characters-xxxxxxxx",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def test_node_credential_is_persisted_atomically_mode_0600(tmp_path: Path) -> None:
    target = tmp_path / "state" / "credential.json"
    credential = _credential()
    store_node_credential(target, credential)
    assert load_node_credential(target) == credential
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(target.parent.glob(".*.tmp"))


def test_enrollment_intent_survives_response_loss_mode_0600(tmp_path: Path) -> None:
    target = tmp_path / ".node.json.enrollment"
    first = enrollment._load_or_create_enrollment_intent(target)
    second = enrollment._load_or_create_enrollment_intent(target)
    assert second == first
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


async def test_source_review_secret_leaves_environment_immediately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credential = tmp_path / "credential.json"
    monkeypatch.setenv("SCREENER_NODE_CREDENTIAL_FILE", str(credential))
    monkeypatch.setenv("SCREENER_SOURCE_REVIEW_API_KEY", "sk-private-test-value")
    await enrollment._materialize_source_review_secret()
    secret_file = Path(os.environ["SCREENER_SOURCE_REVIEW_API_KEY_FILE"])
    assert "SCREENER_SOURCE_REVIEW_API_KEY" not in os.environ
    assert secret_file.read_text().strip() == "sk-private-test-value"
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600


async def test_source_review_secret_uses_short_lived_gcp_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credential = tmp_path / "credential.json"
    monkeypatch.setenv("SCREENER_NODE_CREDENTIAL_FILE", str(credential))
    monkeypatch.setenv("SCREENER_GCP_BOOTSTRAP_ACCESS_TOKEN", "oauth-short-lived")
    monkeypatch.setenv(
        "SCREENER_SOURCE_REVIEW_SECRET_RESOURCE",
        "projects/test/secrets/source-review",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer oauth-short-lived"
        assert request.url.path.endswith(
            "/projects/test/secrets/source-review/versions/latest:access"
        )
        return httpx.Response(
            200,
            json={"payload": {"data": b64encode(b"sk-private-test-value").decode()}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(enrollment.httpx, "AsyncClient", lambda **_kwargs: client)
    await enrollment._materialize_source_review_secret()

    secret_file = Path(os.environ["SCREENER_SOURCE_REVIEW_API_KEY_FILE"])
    assert "SCREENER_GCP_BOOTSTRAP_ACCESS_TOKEN" not in os.environ
    assert "SCREENER_SOURCE_REVIEW_SECRET_RESOURCE" not in os.environ
    assert secret_file.read_text().strip() == "sk-private-test-value"
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
