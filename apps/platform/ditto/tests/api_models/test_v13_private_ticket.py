"""Identity-only ticket tests; no protected cases or production key."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ditto.api_models.v13_private_ticket import (
    V13PrivateCaseClaims,
    issue_private_case_ticket,
)


def _claims() -> V13PrivateCaseClaims:
    now = datetime.now(UTC)
    return V13PrivateCaseClaims(
        group_id=uuid4(),
        role="target",
        agent_id=uuid4(),
        attempt_id=uuid4(),
        artifact_sha256="a" * 64,
        image_sha256="b" * 64,
        image_upload_id=uuid4(),
        profile_sha256="c" * 64,
        manifest_sha256="d" * 64,
        session_id=uuid4(),
        case_id=uuid4(),
        nonce="e" * 64,
        issued_at=now,
        expires_at=now + timedelta(minutes=4),
    )


def test_private_ticket_signs_canonical_exact_identity() -> None:
    claims = _claims()
    key = b"synthetic-private-ticket-key-32-bytes"
    ticket = issue_private_case_ticket(claims=claims, key=key, now=claims.issued_at)
    raw = base64.urlsafe_b64decode(ticket.body + "=" * (-len(ticket.body) % 4))
    assert json.loads(raw)["attempt_id"] == str(claims.attempt_id)
    assert json.loads(raw)["image_sha256"] == claims.image_sha256
    assert hmac.compare_digest(
        ticket.mac_sha256,
        hmac.new(
            key, b"ditto-v13-private-case-ticket-v1\0" + raw, hashlib.sha256
        ).hexdigest(),
    )


def test_private_ticket_rejects_expired_and_weak_key() -> None:
    claims = _claims()
    with pytest.raises(ValueError):
        issue_private_case_ticket(claims=claims, key=b"short", now=claims.issued_at)
    with pytest.raises(ValueError):
        issue_private_case_ticket(
            claims=claims,
            key=b"synthetic-private-ticket-key-32-bytes",
            now=claims.expires_at,
        )
    with pytest.raises(ValueError):
        V13PrivateCaseClaims.model_validate(
            {
                **claims.model_dump(mode="json"),
                "expires_at": (claims.issued_at + timedelta(minutes=6)).isoformat(),
            }
        )
