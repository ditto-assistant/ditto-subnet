"""Canonical signing payload for versioned screener verdicts."""

from __future__ import annotations

from uuid import UUID

from ditto_screening_protocol.models import SCREENING_POLICY_VERSION


def verdict_signing_message(
    *,
    screener_hotkey: str,
    agent_id: UUID,
    passed: bool,
    policy_version: int = SCREENING_POLICY_VERSION,
) -> bytes:
    """Return the exact bytes signed by the screener and verified by the API."""
    return f"{screener_hotkey}:{agent_id}:{passed}:{policy_version}".encode()
