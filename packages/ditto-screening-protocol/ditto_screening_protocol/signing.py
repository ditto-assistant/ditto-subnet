"""Canonical signing payload for versioned screener verdicts."""

from __future__ import annotations

import json
from uuid import UUID

from ditto_screening_protocol.models import (
    SCREENING_POLICY_VERSION,
    TYPED_OUTCOME_POLICY_VERSION,
    ScreenResultOutcome,
)


def verdict_signing_message(
    *,
    screener_hotkey: str,
    agent_id: UUID,
    passed: bool,
    policy_version: int = SCREENING_POLICY_VERSION,
    attempt_id: UUID | None = None,
    outcome: ScreenResultOutcome | None = None,
    manifest_digest: str | None = None,
    finding_digest: str | None = None,
    review_audit_digest: str | None = None,
    adjudication_digest: str | None = None,
    deferred_source_review: bool = False,
    policy_only: bool = False,
    review_settings_revision: int | None = None,
    review_settings_instance_id: str | None = None,
    review_settings_scope: str | None = None,
    review_settings_checksum: str | None = None,
    reason_code: str | None = None,
    image_sha256: str | None = None,
    image_size_bytes: int | None = None,
    image_id: str | None = None,
    image_ref: str | None = None,
    image_upload_id: UUID | None = None,
) -> bytes:
    """Return the exact bytes signed by the screener and verified by the API."""
    if outcome is not None:
        if attempt_id is None:
            raise ValueError("typed result signature requires attempt_id")
        fields: dict[str, str | int | bool | None] = {
            "agent_id": str(agent_id),
            "attempt_id": str(attempt_id),
            "finding_digest": finding_digest,
            "image_id": image_id,
            "image_ref": image_ref,
            "image_sha256": image_sha256,
            "image_size_bytes": image_size_bytes,
            "image_upload_id": (
                str(image_upload_id) if image_upload_id is not None else None
            ),
            "manifest_digest": manifest_digest,
            "outcome": outcome.value,
            "policy_version": policy_version,
            "reason_code": reason_code,
            "screener_hotkey": screener_hotkey,
        }
        if review_settings_revision is not None:
            fields.update(
                {
                    "review_settings_checksum": review_settings_checksum,
                    "review_settings_instance_id": review_settings_instance_id,
                    "review_settings_revision": review_settings_revision,
                    "review_settings_scope": review_settings_scope,
                }
            )
        if review_audit_digest is not None:
            fields["review_audit_digest"] = review_audit_digest
        if adjudication_digest is not None:
            fields["adjudication_digest"] = adjudication_digest
        # Omit the false default so mixed-version signatures remain byte-for-byte
        # compatible. A true deferred claim is explicitly bound against replay.
        if deferred_source_review:
            fields["deferred_source_review"] = True
        if policy_only:
            fields["policy_only"] = True
        payload = json.dumps(
            fields,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"ditto-screen-result:v5:{payload}".encode()
    if policy_version >= TYPED_OUTCOME_POLICY_VERSION:
        raise ValueError("policy v9+ verdict requires typed outcome")
    if any(
        value is not None
        for value in (
            image_sha256,
            image_size_bytes,
            image_id,
            image_ref,
            image_upload_id,
        )
    ):
        raise ValueError("legacy verdict cannot carry screened image metadata")
    if attempt_id is not None:
        return (
            "ditto-screen-verdict:v2:"
            f"{screener_hotkey}:{agent_id}:{attempt_id}:{passed}:{policy_version}"
        ).encode()
    return f"{screener_hotkey}:{agent_id}:{passed}:{policy_version}".encode()
