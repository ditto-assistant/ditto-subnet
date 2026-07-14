"""Guard the dedicated screener heartbeat against cross-repo drift."""

from __future__ import annotations

import json
from pathlib import Path

from ditto.tests.contract._schema import (
    SHARED_SCREENER_HEARTBEAT_MODELS,
    compute_contract,
)

_GOLDEN = Path(__file__).parent / "screener_heartbeat_contract.json"


def test_screener_heartbeat_models_match_platform_contract() -> None:
    golden = json.loads(_GOLDEN.read_text())
    actual = compute_contract(
        models=SHARED_SCREENER_HEARTBEAT_MODELS,
        module="ditto.api_models.screener",
    )
    assert set(actual) == set(golden) == set(SHARED_SCREENER_HEARTBEAT_MODELS)
    mismatched = [
        name
        for name in SHARED_SCREENER_HEARTBEAT_MODELS
        if actual[name] != golden[name]
    ]
    assert not mismatched, (
        f"screener heartbeat model(s) {mismatched} drifted from the platform; "
        "regenerate screener_heartbeat_contract.json from the matching "
        "ditto-platform branch"
    )
