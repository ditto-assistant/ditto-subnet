"""Verify short-lived Backroom assertions for V13 human control attestations.

The shared admin bearer and X-Admin-Actor are insufficient to identify a human.
Only Backroom's authenticated Google session may mint this separate assertion.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import HTTPException

_SUB_RE = re.compile(r"^[A-Za-z0-9:_-]{1,120}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_AUDIENCE = "ditto-platform-v13-benign-approval"
BenignAssertionAction = Literal["attest-known-benign", "authorize-generation"]


@dataclass(frozen=True)
class VerifiedBenignPrincipal:
    sub: str
    email: str
    assertion_sha256: str


def verify_v13_benign_assertion(
    assertion: str,
    *,
    secret: str | None,
    approval_id: UUID,
    evidence_sha256: str,
    action: BenignAssertionAction,
    now: int | None = None,
) -> VerifiedBenignPrincipal:
    if not secret:
        raise HTTPException(status_code=503, detail="V13 human attestations disabled")
    if len(assertion) > 2048:
        raise HTTPException(status_code=401, detail="invalid V13 attestation")
    try:
        version, encoded, signature = assertion.split(".")
        if version != "v1" or not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise ValueError("version or signature")
        signed = f"v1.{encoded}".encode("ascii")
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        padded = encoded + "=" * (-len(encoded) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        if not isinstance(claims, dict) or set(claims) != {
            "aud",
            "action",
            "approval_id",
            "evidence_sha256",
            "sub",
            "email",
            "iat",
            "nonce",
        }:
            raise ValueError("claims")
        current = int(time.time()) if now is None else now
        if (
            claims["aud"] != _AUDIENCE
            or claims["action"] != action
            or claims["approval_id"] != str(approval_id)
            or claims["evidence_sha256"] != evidence_sha256
            or type(claims["iat"]) is not int
            or not -5 <= current - claims["iat"] <= 120
            or not isinstance(claims["sub"], str)
            or not _SUB_RE.fullmatch(claims["sub"])
            or claims["sub"] == "sn118-preview"
            or not isinstance(claims["email"], str)
            or not 3 <= len(claims["email"]) <= 254
            or "@" not in claims["email"]
            or not isinstance(claims["nonce"], str)
            or not _NONCE_RE.fullmatch(claims["nonce"])
        ):
            raise ValueError("claims")
    except (UnicodeError, ValueError, TypeError, KeyError, binascii.Error):
        raise HTTPException(status_code=401, detail="invalid V13 attestation") from None
    return VerifiedBenignPrincipal(
        sub=claims["sub"],
        email=claims["email"].lower(),
        assertion_sha256=hashlib.sha256(assertion.encode()).hexdigest(),
    )
