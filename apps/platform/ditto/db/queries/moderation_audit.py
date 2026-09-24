"""Signed, public-safe audit records for moderation eligibility changes.

Each record is an append-only ``moderation`` event on the existing score audit
chain, so it shares that chain's root and is exported by the public audit
feed. The signature is an Ed25519 role key, not an operator identity. Reviewer
notes, evidence, credentials, source, request bodies, and operator names are
not fields of the signed body and are dropped if a caller tries to attach them.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import ScoreAuditEntry
from ditto.db.queries.audit import append_audit_entry

EVENT_MODERATION = "moderation"
SCHEMA_VERSION = 1
_SIGNING_KEY_ENV = "DITTO_MODERATION_AUDIT_SIGNING_KEY"
_PREVIOUS_KEYS_ENV = "DITTO_MODERATION_AUDIT_PREVIOUS_PUBLIC_KEYS"

ACTION_RELEASE = "release"
ACTION_REJECT = "reject"
ACTION_RESCREEN = "rescreen"
ACTION_QUARANTINE = "quarantine"
ACTION_PROVENANCE_REVOCATION = "provenance_revocation"
ACTION_ARTIFACT_SUPERSESSION = "artifact_supersession"

ACTION_TYPES = frozenset(
    {
        ACTION_RELEASE,
        ACTION_REJECT,
        ACTION_RESCREEN,
        ACTION_QUARANTINE,
        ACTION_PROVENANCE_REVOCATION,
        ACTION_ARTIFACT_SUPERSESSION,
    }
)

PUBLIC_REASON_CODES = {
    ACTION_RELEASE: "operator_release",
    ACTION_REJECT: "operator_reject",
    ACTION_RESCREEN: "operator_rescreen",
    ACTION_QUARANTINE: "operator_quarantine",
    ACTION_PROVENANCE_REVOCATION: "provenance_revoked",
    ACTION_ARTIFACT_SUPERSESSION: "artifact_superseded",
}

SIGNED_FIELDS = (
    "schema_version",
    "action_id",
    "action_type",
    "recorded_at",
    "agent_id",
    "miner_hotkey",
    "artifact_sha256",
    "screened_image_sha256",
    "previous_status",
    "resulting_status",
    "reason_code",
    "signer_key_id",
    "related_action_id",
)

PREVIEW_FIELDS = (
    "schema_version",
    "action_type",
    "artifact_sha256",
    "screened_image_sha256",
    "previous_status",
    "resulting_status",
    "reason_code",
)

_SENSITIVE_KEYS = frozenset(
    {
        "actor",
        "body",
        "credential",
        "credentials",
        "email",
        "evidence",
        "note",
        "notes",
        "operator",
        "operator_email",
        "reason",
        "request",
        "reviewer_note",
        "source",
        "token",
    }
)


class ModerationAuditUnavailable(Exception):
    """The role key is missing, so no public moderation record can be published."""


class DuplicateModerationAction(Exception):
    """This action id is already on the append-only chain."""


@dataclass(frozen=True)
class _SignerState:
    private: Ed25519PrivateKey | None
    previous: tuple[bytes, ...]
    loaded: bool


_state = _SignerState(private=None, previous=(), loaded=False)


def reset_moderation_signer() -> None:
    """Drop the cached role key so the next call re-reads the environment."""
    global _state
    _state = _SignerState(private=None, previous=(), loaded=False)


def configure_moderation_signer(
    private: Ed25519PrivateKey | None,
    *,
    previous: tuple[bytes, ...] = (),
) -> None:
    """Install a signer for this process. ``None`` means publishing is unavailable."""
    global _state
    _state = _SignerState(private=private, previous=previous, loaded=True)


def _canonical_bytes(content: dict[str, Any]) -> bytes:
    return json.dumps(content, sort_keys=True, separators=(",", ":")).encode()


def _public_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _key_id(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()


def _parse_public_key(text: str) -> bytes:
    raw = bytes.fromhex(text.strip())
    if len(raw) != 32:
        raise ValueError("ed25519 public key must be 32 bytes")
    Ed25519PublicKey.from_public_bytes(raw)
    return raw


def _load_signer() -> _SignerState:
    global _state
    if _state.loaded:
        return _state
    seed = os.environ.get(_SIGNING_KEY_ENV, "").strip()
    private: Ed25519PrivateKey | None = None
    if seed:
        raw = bytes.fromhex(seed)
        if len(raw) != 32:
            raise ModerationAuditUnavailable("moderation signing key is malformed")
        private = Ed25519PrivateKey.from_private_bytes(raw)
    previous: list[bytes] = []
    for item in os.environ.get(_PREVIOUS_KEYS_ENV, "").split(","):
        if item.strip():
            previous.append(_parse_public_key(item))
    _state = _SignerState(private=private, previous=tuple(previous), loaded=True)
    return _state


def published_signer_public_keys() -> list[str]:
    """Hex public keys a client may trust. The current key is first."""
    state = _load_signer()
    keys: list[bytes] = []
    if state.private is not None:
        keys.append(_public_bytes(state.private.public_key()))
    keys.extend(state.previous)
    published: list[str] = []
    for key in keys:
        text = key.hex()
        if text not in published:
            published.append(text)
    return published


def _trusted_keys() -> dict[str, bytes]:
    trusted: dict[str, bytes] = {}
    for text in published_signer_public_keys():
        raw = bytes.fromhex(text)
        trusted[_key_id(raw)] = raw
    return trusted


def public_status(status: object) -> str:
    value = getattr(status, "value", status)
    return str(value)


def preview_moderation_record(
    *,
    action_type: str,
    artifact_sha256: str,
    screened_image_sha256: str | None,
    previous_status: str,
    resulting_status: str,
) -> tuple[str, str]:
    """Return the public reason code and the hash of the fields known before commit."""
    if action_type not in ACTION_TYPES:
        raise ValueError(f"unknown moderation action {action_type}")
    reason_code = PUBLIC_REASON_CODES[action_type]
    body = {
        "schema_version": SCHEMA_VERSION,
        "action_type": action_type,
        "artifact_sha256": artifact_sha256,
        "screened_image_sha256": screened_image_sha256,
        "previous_status": previous_status,
        "resulting_status": resulting_status,
        "reason_code": reason_code,
    }
    return reason_code, hashlib.sha256(_canonical_bytes(body)).hexdigest()


def redact_moderation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only the public moderation fields. Sensitive keys never survive."""
    kept = {key: payload[key] for key in SIGNED_FIELDS if key in payload}
    if "signature" in payload:
        kept["signature"] = payload["signature"]
    return kept


def moderation_signature_body(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in SIGNED_FIELDS}


def verify_moderation_payload(
    payload: dict[str, Any],
    *,
    trusted_public_keys: list[str] | None = None,
) -> bool:
    """True when ``payload`` is signed by a published role key and is fully public."""
    if any(key in payload for key in _SENSITIVE_KEYS):
        return False
    signature = payload.get("signature")
    signer_key_id = payload.get("signer_key_id")
    if not isinstance(signature, str) or not isinstance(signer_key_id, str):
        return False
    if trusted_public_keys is None:
        trusted = _trusted_keys()
    else:
        trusted = {}
        for item in trusted_public_keys:
            raw = bytes.fromhex(item)
            trusted[_key_id(raw)] = raw
    public = trusted.get(signer_key_id)
    if public is None:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(
            bytes.fromhex(signature),
            _canonical_bytes(moderation_signature_body(payload)),
        )
    except (InvalidSignature, ValueError):
        return False
    return True


async def latest_moderation_action_id(
    session: AsyncSession,
    *,
    agent_id: UUID,
    action_type: str,
) -> str | None:
    row = await session.scalar(
        select(ScoreAuditEntry)
        .where(
            ScoreAuditEntry.agent_id == agent_id,
            ScoreAuditEntry.event == EVENT_MODERATION,
            ScoreAuditEntry.payload["action_type"].as_string() == action_type,
        )
        .order_by(ScoreAuditEntry.seq.desc())
        .limit(1)
    )
    if row is None:
        return None
    action_id = row.payload.get("action_id")
    return action_id if isinstance(action_id, str) else None


async def record_moderation_audit(
    session: AsyncSession,
    *,
    action_type: str,
    agent_id: UUID,
    miner_hotkey: str | None,
    artifact_sha256: str,
    screened_image_sha256: str | None,
    previous_status: str,
    resulting_status: str,
    recorded_at: datetime,
    related_action_id: str | None = None,
    action_id: UUID | None = None,
) -> ScoreAuditEntry:
    """Append one signed moderation event inside the state-change transaction."""
    if action_type not in ACTION_TYPES:
        raise ValueError(f"unknown moderation action {action_type}")
    state = _load_signer()
    if state.private is None:
        raise ModerationAuditUnavailable("moderation signing key is not configured")
    chosen = action_id or uuid4()
    existing = await session.scalar(
        select(ScoreAuditEntry.seq)
        .where(
            ScoreAuditEntry.event == EVENT_MODERATION,
            ScoreAuditEntry.payload["action_id"].as_string() == str(chosen),
        )
        .limit(1)
    )
    if existing is not None:
        raise DuplicateModerationAction(str(chosen))
    public_key = _public_bytes(state.private.public_key())
    stamped = recorded_at if recorded_at.tzinfo else recorded_at.replace(tzinfo=UTC)
    body = {
        "schema_version": SCHEMA_VERSION,
        "action_id": str(chosen),
        "action_type": action_type,
        "recorded_at": stamped.astimezone(UTC).isoformat(),
        "agent_id": str(agent_id),
        "miner_hotkey": miner_hotkey,
        "artifact_sha256": artifact_sha256,
        "screened_image_sha256": screened_image_sha256,
        "previous_status": previous_status,
        "resulting_status": resulting_status,
        "reason_code": PUBLIC_REASON_CODES[action_type],
        "signer_key_id": _key_id(public_key),
        "related_action_id": related_action_id,
    }
    body = redact_moderation_payload(body)
    signature = state.private.sign(
        _canonical_bytes(moderation_signature_body(body))
    ).hex()
    payload = redact_moderation_payload({**body, "signature": signature})
    entry = await append_audit_entry(
        session,
        agent_id=agent_id,
        validator_hotkey=None,
        event=EVENT_MODERATION,
        payload=payload,
        recorded_at=recorded_at,
    )
    return entry


__all__ = [
    "ACTION_ARTIFACT_SUPERSESSION",
    "ACTION_PROVENANCE_REVOCATION",
    "ACTION_QUARANTINE",
    "ACTION_REJECT",
    "ACTION_RELEASE",
    "ACTION_RESCREEN",
    "EVENT_MODERATION",
    "DuplicateModerationAction",
    "ModerationAuditUnavailable",
    "configure_moderation_signer",
    "latest_moderation_action_id",
    "preview_moderation_record",
    "public_status",
    "published_signer_public_keys",
    "record_moderation_audit",
    "redact_moderation_payload",
    "reset_moderation_signer",
    "verify_moderation_payload",
]
