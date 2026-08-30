from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from ditto.api_models.coding_claims import (
    CodingClaimResponse,
    coding_claim_action_signing_message,
    coding_claim_next_signing_message,
)

_NOW = datetime(2026, 8, 23, 20, tzinfo=UTC)
_TICKET = UUID("33333333-3333-4333-8333-333333333333")


def _response(**updates) -> dict:
    value = {
        "schema": "dittobench-coding-ticket-claim-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "validator_hotkey": "5" + "V" * 47,
        "instance_id": "coding-worker-instance-001",
        "claim_generation": 1,
        "claim_expires_at": _NOW + timedelta(minutes=2),
        "claim_started_at": None,
        "idempotent": False,
        "agent_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "run_row_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "ticket_id": str(_TICKET),
        "ticket_deadline": _NOW + timedelta(hours=1),
        "bench_version": 12,
        "coding_run_id": "coding-run-001",
        "agent_artifact_sha256": "aa" * 32,
        "screened_image_sha256": "bb" * 32,
        "run_manifest_sha256": "cc" * 32,
        "task_set_manifest_sha256": "dd" * 32,
    }
    value.update(updates)
    return value


def test_claim_response_is_shadow_only_and_coherent() -> None:
    claim = CodingClaimResponse.model_validate(_response())
    assert claim.weight_eligible is False
    assert claim.ticket_id == _TICKET
    assert claim.claim_started_at is None
    for field, value in (
        ("claim_expires_at", _NOW + timedelta(hours=2)),
        ("claim_started_at", _NOW + timedelta(hours=1)),
        ("claim_generation", 0),
    ):
        with pytest.raises(ValidationError):
            CodingClaimResponse.model_validate(_response(**{field: value}))
    with pytest.raises(ValidationError):
        CodingClaimResponse.model_validate(
            _response(claim_started_at=_NOW + timedelta(minutes=2))
        )


def test_claim_signing_domains_are_action_and_generation_separated() -> None:
    nonce = UUID("11111111-1111-4111-8111-111111111111")
    next_message = coding_claim_next_signing_message(
        validator_hotkey="5" + "V" * 47,
        instance_id="coding-worker-instance-001",
        nonce=nonce,
        requested_at=_NOW,
    )
    start = coding_claim_action_signing_message(
        action="start",
        validator_hotkey="5" + "V" * 47,
        instance_id="coding-worker-instance-001",
        ticket_id=_TICKET,
        claim_generation=1,
        nonce=nonce,
        requested_at=_NOW,
    )
    heartbeat = coding_claim_action_signing_message(
        action="heartbeat",
        validator_hotkey="5" + "V" * 47,
        instance_id="coding-worker-instance-001",
        ticket_id=_TICKET,
        claim_generation=1,
        nonce=nonce,
        requested_at=_NOW,
    )
    assert len({next_message, start, heartbeat}) == 3
    assert str(_TICKET).encode() not in next_message
    assert b"\x001\x00" in start
