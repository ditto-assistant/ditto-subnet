import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from ditto_screening_protocol.contributor_identity import (
    ClaPublishRequest,
    ClaSignRequest,
    GithubLinkRequest,
    GithubLogin,
    GithubUnlinkRequest,
    cla_message,
    github_link_message,
    github_unlink_message,
)


@pytest.mark.parametrize("login", ["a", "miner-fixture", "A--A", "c--", "a" * 39])
def test_github_login_preserves_valid_legacy_accounts(login):
    assert TypeAdapter(GithubLogin).validate_python(login) == login


@pytest.mark.parametrize("login", ["", "-miner", "miner_name", "a" * 40, "a/b", "a:b"])
def test_github_login_rejects_unsafe_or_out_of_bounds_labels(login):
    with pytest.raises(ValueError):
        TypeAdapter(GithubLogin).validate_python(login)


_HOTKEY = "5" + "F" * 47
_SIGNATURE = "ab" * 64
_SIGNED_REQUESTS = {
    GithubLinkRequest: {
        "hotkey": _HOTKEY,
        "github_user_id": 1,
        "github_login": "fixture",
        "link_attempt_id": str(UUID(int=1)),
        "nonce": str(UUID(int=2)),
        "issued_at": "2026-10-15T00:00:00Z",
        "proof": {"key_kind": "hotkey", "signer": _HOTKEY, "signature": _SIGNATURE},
    },
    GithubUnlinkRequest: {
        "hotkey": _HOTKEY,
        "link_id": str(UUID(int=1)),
        "nonce": str(UUID(int=2)),
        "issued_at": "2026-10-15T00:00:00Z",
        "proof": {"key_kind": "hotkey", "signer": _HOTKEY, "signature": _SIGNATURE},
    },
    ClaSignRequest: {
        "hotkey": _HOTKEY,
        "cla_version": 1,
        "cla_sha256": "a" * 64,
        "nonce": str(UUID(int=2)),
        "issued_at": "2026-10-15T00:00:00Z",
        "signature": _SIGNATURE,
    },
}


@pytest.mark.parametrize("model", list(_SIGNED_REQUESTS), ids=lambda m: m.__name__)
def test_signed_requests_accept_integer_netuid(model):
    body = json.dumps({**_SIGNED_REQUESTS[model], "netuid": 118})
    assert model.model_validate_json(body).netuid == 118


@pytest.mark.parametrize("netuid", [True, "118", 118.0, -1])
@pytest.mark.parametrize("model", list(_SIGNED_REQUESTS), ids=lambda m: m.__name__)
def test_signed_requests_reject_coerced_netuid(model, netuid):
    # The netuid is part of the signed message; a boolean would sign as 1.
    body = json.dumps({**_SIGNED_REQUESTS[model], "netuid": netuid})
    with pytest.raises(ValidationError, match="netuid"):
        model.model_validate_json(body)


@pytest.mark.parametrize("text", ["a" * 262144, "猫" * (262144 // 3)])
def test_cla_publish_accepts_text_within_utf8_byte_limit(text):
    assert (
        ClaPublishRequest(
            version=1,
            sha256="a" * 64,
            text=text,
            effective_at=datetime.now(UTC),
            reason="fixture publication",
            confirmation="fixture confirmation",
        ).text
        == text
    )


def test_cla_publish_rejects_multibyte_text_over_byte_limit():
    text = "猫" * (262144 // 3 + 1)
    assert len(text) < 262144
    with pytest.raises(ValueError, match="UTF-8 limit"):
        ClaPublishRequest(
            version=1,
            sha256="a" * 64,
            text=text,
            effective_at=datetime.now(UTC),
            reason="fixture publication",
            confirmation="fixture confirmation",
        )


@pytest.mark.parametrize("kind", ["cla", "link", "unlink"])
def test_signing_helpers_reject_naive_timestamps(kind):
    issued = datetime(2026, 10, 13)
    nonce = UUID(int=1)
    with pytest.raises(ValueError, match="timezone-aware"):
        if kind == "cla":
            cla_message(
                netuid=118,
                cla_version=1,
                cla_sha256="a" * 64,
                hotkey="fixture",
                nonce=nonce,
                issued_at=issued,
            )
        elif kind == "link":
            github_link_message(
                netuid=118,
                hotkey="fixture",
                github_user_id=1,
                github_login="fixture",
                link_attempt_id=nonce,
                nonce=nonce,
                issued_at=issued,
                key_kind="hotkey",
                signer="fixture",
            )
        else:
            github_unlink_message(
                netuid=118,
                hotkey="fixture",
                link_id=nonce,
                nonce=nonce,
                issued_at=issued,
                key_kind="hotkey",
                signer="fixture",
            )


def test_aware_signing_bytes_remain_pinned():
    payload = cla_message(
        netuid=118,
        cla_version=1,
        cla_sha256="a" * 64,
        hotkey="fixture",
        nonce=UUID(int=1),
        issued_at=datetime(2026, 10, 13, tzinfo=UTC),
    )
    assert payload == (
        b"ditto-cla:v1:118:1:"
        + b"a" * 64
        + b":fixture:00000000-0000-0000-0000-000000000001:2026-10-13T00:00:00.000000+00:00"
    )
