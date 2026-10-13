"""Tests for Miner CLI & Platform GitHub link attestation (#2871)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from apps.platform.ditto.api_server.github_link import (
    GitHubLinkRejectedError,
    GitHubLinkService,
    github_link_message as server_link_msg,
)
from ditto.miner_cli.github_link import github_link_message as cli_link_msg


def _fake_sign(signer: str, payload: bytes) -> str:
    return hashlib.sha256(signer.encode() + b":" + payload).hexdigest()


def _fake_verify(*, signer: str, payload: bytes, signature_hex: str) -> bool:
    return _fake_sign(signer, payload) == signature_hex


def test_access_token_is_never_persisted_logged_or_returned() -> None:
    svc = GitHubLinkService()
    secret_token = "gho_SUPER_SECRET_EPHEMERAL_OAUTH_TOKEN_999"
    attempt_id = uuid.uuid4()
    resp = svc.complete_oauth_callback(
        attempt_id=attempt_id,
        hotkey="5HotkeyA",
        oauth_code="code_123",
        exchange_code_fn=lambda _: {"access_token": secret_token},
        fetch_user_fn=lambda tok: {"id": 424242, "login": "alice-dev"},
    )
    assert secret_token not in str(resp)
    assert secret_token not in str(svc.__dict__)
    assert resp["github_user_id"] == 424242
    assert resp["github_login"] == "alice-dev"


def test_link_rejections_and_unlink_preserves_history() -> None:
    svc = GitHubLinkService()
    attempt_a = uuid.uuid4()
    hotkey_a = "5HotkeyA"
    hotkey_b = "5HotkeyB"
    now = datetime.now(UTC)

    svc.complete_oauth_callback(
        attempt_id=attempt_a,
        hotkey=hotkey_a,
        oauth_code="c1",
        exchange_code_fn=lambda _: {"access_token": "tok"},
        fetch_user_fn=lambda _: {"id": 1001, "login": "miner-one"},
    )

    # 1. Wrong signer rejected
    n1 = uuid.uuid4()
    p1 = cli_link_msg(
        netuid=118,
        hotkey=hotkey_a,
        github_user_id=1001,
        github_login="miner-one",
        link_attempt_id=attempt_a,
        nonce=n1,
        issued_at=now,
        key_kind="hotkey",
        signer=hotkey_a,
    )
    assert p1 == server_link_msg(
        netuid=118,
        hotkey=hotkey_a,
        github_user_id=1001,
        github_login="miner-one",
        link_attempt_id=attempt_a,
        nonce=n1,
        issued_at=now,
        key_kind="hotkey",
        signer=hotkey_a,
    )

    with pytest.raises(GitHubLinkRejectedError, match="wrong signer"):
        svc.finalize_signed_link(
            netuid=118,
            hotkey=hotkey_a,
            link_attempt_id=attempt_a,
            key_kind="hotkey",
            signer="5WrongHotkey",
            signature_hex=_fake_sign(hotkey_a, p1),
            nonce=n1,
            issued_at=now,
            verify_fn=_fake_verify,
            resolve_owner_coldkey_fn=lambda _: "5ColdA",
            now=now,
        )

    # 2. Stale issued_at rejected
    stale = now - timedelta(minutes=30)
    with pytest.raises(GitHubLinkRejectedError, match="stale issued_at"):
        svc.finalize_signed_link(
            netuid=118,
            hotkey=hotkey_a,
            link_attempt_id=attempt_a,
            key_kind="hotkey",
            signer=hotkey_a,
            signature_hex=_fake_sign(hotkey_a, p1),
            nonce=uuid.uuid4(),
            issued_at=stale,
            verify_fn=_fake_verify,
            resolve_owner_coldkey_fn=lambda _: "5ColdA",
            now=now,
        )

    # 3. Valid link succeeds
    svc.finalize_signed_link(
        netuid=118,
        hotkey=hotkey_a,
        link_attempt_id=attempt_a,
        key_kind="hotkey",
        signer=hotkey_a,
        signature_hex=_fake_sign(hotkey_a, p1),
        nonce=n1,
        issued_at=now,
        verify_fn=_fake_verify,
        resolve_owner_coldkey_fn=lambda _: "5ColdA",
        now=now,
    )
    assert svc.public_read(hotkey_a)["github_user_id"] == 1001

    # 4. Replayed nonce rejected
    with pytest.raises(GitHubLinkRejectedError, match="replayed nonce"):
        svc.finalize_signed_link(
            netuid=118,
            hotkey=hotkey_a,
            link_attempt_id=attempt_a,
            key_kind="hotkey",
            signer=hotkey_a,
            signature_hex=_fake_sign(hotkey_a, p1),
            nonce=n1,
            issued_at=now,
            verify_fn=_fake_verify,
            resolve_owner_coldkey_fn=lambda _: "5ColdA",
            now=now,
        )

    # 5. Another hotkey trying to link the same numeric github_user_id (1001) is rejected
    attempt_b = uuid.uuid4()
    svc.complete_oauth_callback(
        attempt_id=attempt_b,
        hotkey=hotkey_b,
        oauth_code="c2",
        exchange_code_fn=lambda _: {"access_token": "tok2"},
        fetch_user_fn=lambda _: {"id": 1001, "login": "miner-one"},
    )
    nb = uuid.uuid4()
    pb = server_link_msg(
        netuid=118,
        hotkey=hotkey_b,
        github_user_id=1001,
        github_login="miner-one",
        link_attempt_id=attempt_b,
        nonce=nb,
        issued_at=now,
        key_kind="hotkey",
        signer=hotkey_b,
    )
    with pytest.raises(GitHubLinkRejectedError, match="already linked to another active hotkey"):
        svc.finalize_signed_link(
            netuid=118,
            hotkey=hotkey_b,
            link_attempt_id=attempt_b,
            key_kind="hotkey",
            signer=hotkey_b,
            signature_hex=_fake_sign(hotkey_b, pb),
            nonce=nb,
            issued_at=now,
            verify_fn=_fake_verify,
            resolve_owner_coldkey_fn=lambda _: "5ColdB",
            now=now,
        )
