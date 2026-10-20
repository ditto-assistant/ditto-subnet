"""Validator wire compatibility for the shared shadow-only treasury pin."""

import json
from pathlib import Path

from ditto.api_models.validator import LedgerResponse
from ditto_screening_protocol.treasury import TreasuryLedgerPin

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "packages/ditto-screening-protocol/tests/fixtures/treasury_ledger_pin_v1.json"
)


def test_legacy_ledger_omits_treasury_field_even_without_exclude_none():
    legacy = LedgerResponse(entries=[], count=0)
    explicit_null = LedgerResponse(entries=[], count=0, treasury_pin=None)
    assert "treasury_pin" not in legacy.model_dump(mode="json")
    assert legacy.model_dump_json() == explicit_null.model_dump_json()


def test_shared_treasury_fixture_roundtrips_on_validator_wire():
    raw = json.loads(FIXTURE.read_text())
    response = LedgerResponse.model_validate(
        {"entries": [], "count": 0, "treasury_pin": raw}
    )
    assert isinstance(response.treasury_pin, TreasuryLedgerPin)
    assert response.model_dump(mode="json")["treasury_pin"] == raw
