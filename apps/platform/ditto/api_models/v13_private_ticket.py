"""Short-lived, exact-identity ticket for a report-only V13 private case.

Only a Platform endpoint that has checked immutable group registration and the
verified image row may call ``issue_private_case_ticket``. This module does
not expose that endpoint, authorize a verdict, or carry protected case bytes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

_DOMAIN = b"ditto-v13-private-case-ticket-v1\0"
_SHA = r"^[0-9a-f]{64}$"


class V13PrivateCaseClaims(BaseModel):
    """Identity-only authorization for one scorer session and case."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: Literal["v13-private-case-ticket-v1"] = "v13-private-case-ticket-v1"
    group_id: UUID
    role: Literal["target", "known_benign"]
    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: str = Field(pattern=_SHA)
    image_sha256: str = Field(pattern=_SHA)
    image_upload_id: UUID
    profile_sha256: str = Field(pattern=_SHA)
    manifest_sha256: str = Field(pattern=_SHA)
    session_id: UUID
    case_id: UUID
    nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def bounded_lifetime(self) -> V13PrivateCaseClaims:
        if (
            self.issued_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or not self.issued_at < self.expires_at
            or self.expires_at - self.issued_at > timedelta(minutes=5)
        ):
            raise ValueError("private case ticket lifetime invalid")
        return self


class V13PrivateCaseTicket(BaseModel):
    """Opaque canonical body and its domain-separated HMAC-SHA256."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    body: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    mac_sha256: str = Field(pattern=_SHA)


class V13PrivateCaseTicketRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    session_id: UUID
    case_id: UUID


def issue_private_case_ticket(
    *,
    claims: V13PrivateCaseClaims,
    key: bytes,
    now: datetime | None = None,
) -> V13PrivateCaseTicket:
    """Sign only already-authorized Platform identities; never log the key."""
    current = now or datetime.now(UTC)
    if (
        len(key) < 32
        or current.tzinfo is None
        or claims.issued_at > current
        or claims.expires_at <= current
        or claims.nonce == "0" * 64
    ):
        raise ValueError("private case ticket issuance unavailable")
    raw = json.dumps(
        claims.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    mac = hmac.new(key, _DOMAIN + raw, hashlib.sha256).hexdigest()
    return V13PrivateCaseTicket(body=body, mac_sha256=mac)


def new_private_case_nonce() -> str:
    return secrets.token_hex(32)
