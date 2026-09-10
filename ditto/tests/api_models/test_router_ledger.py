"""Router score ledger wire model: parse discipline + forward compatibility."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from ditto.api_models.router_ledger import (
    RouterHarness,
    RouterHarnessResult,
    RouterLedgerEntry,
    RouterLedgerResponse,
)

_AGENT = UUID("44444444-4444-4444-8444-444444444444")
_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _harness_payload(harness: str = "claude_code") -> dict[str, object]:
    return {
        "harness": harness,
        "operational": True,
        "floor_pass": True,
        "efficiency": 0.75,
        "upstream_token_cost_micros": 1234,
    }


def _entry_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "miner_hotkey": "5" * 48,
        "agent_id": str(_AGENT),
        "router_contract_version": 1,
        "weight_eligible": False,
        "combined_score": 0.5,
        "harnesses": [_harness_payload(h.value) for h in RouterHarness],
        "first_seen": _T0.isoformat(),
    }
    payload.update(overrides)
    return payload


def test_entry_round_trips_and_normalizes_first_seen() -> None:
    entry = RouterLedgerEntry.model_validate(_entry_payload())
    assert entry.combined_score == 0.5
    assert entry.first_seen.tzinfo is UTC
    assert len(entry.harnesses) == 4


def test_entry_ignores_unknown_fields_forward_compatible() -> None:
    entry = RouterLedgerEntry.model_validate(
        _entry_payload(future_field="ignored", another=123)
    )
    assert entry.miner_hotkey == "5" * 48


def test_naive_first_seen_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        RouterLedgerEntry.model_validate(
            _entry_payload(first_seen="2026-01-01T00:00:00")
        )


def test_nil_agent_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="nil"):
        RouterLedgerEntry.model_validate(
            _entry_payload(agent_id="00000000-0000-0000-0000-000000000000")
        )


def test_repeated_harness_is_rejected() -> None:
    with pytest.raises(ValidationError, match="repeats a harness"):
        RouterLedgerEntry.model_validate(
            _entry_payload(
                harnesses=[_harness_payload("grok"), _harness_payload("grok")]
            )
        )


def test_efficiency_out_of_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RouterHarnessResult.model_validate({**_harness_payload(), "efficiency": 1.5})


def test_empty_response_is_the_shadow_default() -> None:
    response = RouterLedgerResponse()
    assert response.entries == []
    assert response.stale is False
    assert response.count == 0
    assert response.router_contract_version is None


def test_response_round_trips_entries_and_metadata() -> None:
    response = RouterLedgerResponse.model_validate(
        {
            "entries": [_entry_payload()],
            "router_contract_version": 1,
            "generated_at": _T0.isoformat(),
            "stale": True,
            "count": 1,
        }
    )
    assert len(response.entries) == 1
    assert response.stale is True
    assert response.count == 1
