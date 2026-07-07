"""Guard: the screener worker's wire models match the platform's contract.

Fails when this repo's ``ditto/api_models/screener.py`` drifts structurally
from the committed golden (the platform's models). A failure means either a
real, intended contract change — in which case regenerate the golden from the
platform with ``scripts/gen_screener_contract.py`` and commit it alongside the
model edit — or an accidental divergence that would break the running screener
against the live API.
"""

from __future__ import annotations

import json
from pathlib import Path

from ditto.tests.contract._schema import SCREENER_MODELS, compute_screener_contract

_GOLDEN = Path(__file__).parent / "screener_contract.json"


def test_screener_models_match_platform_contract() -> None:
    golden = json.loads(_GOLDEN.read_text())
    actual = compute_screener_contract()

    # Per-model diff first so a failure names the offending model, not just
    # "the big dict differs".
    assert set(actual) == set(golden) == set(SCREENER_MODELS), (
        "shared screener model set changed; update SCREENER_MODELS + golden"
    )
    mismatched = [name for name in SCREENER_MODELS if actual[name] != golden[name]]
    assert not mismatched, (
        f"screener wire model(s) {mismatched} drifted from the platform "
        f"contract. If intended, regenerate ditto/tests/contract/"
        f"screener_contract.json from ditto-platform via "
        f"scripts/gen_screener_contract.py and commit it with the change."
    )
