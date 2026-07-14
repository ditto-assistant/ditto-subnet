"""Guard: the validator client's wire models match the platform's contract.

Fails when this repo's ``ditto/api_models/validator.py`` drifts structurally
from the committed golden (the platform's models). A failure means either a
real, intended contract change — in which case regenerate the golden from the
platform with ``scripts/gen_validator_contract.py`` and commit it alongside the
model edit — or an accidental divergence that would break the running validator
against the live API.
"""

from __future__ import annotations

import json
from pathlib import Path

from ditto.api_models.agent_status import AgentStatus
from ditto.tests.contract._schema import SHARED_MODELS, compute_contract

_GOLDEN = Path(__file__).parent / "validator_contract.json"


def test_validator_models_match_platform_contract() -> None:
    golden = json.loads(_GOLDEN.read_text())
    actual = compute_contract()

    # Per-model diff first so a failure names the offending model, not just
    # "the big dict differs".
    assert set(actual) == set(golden) == set(SHARED_MODELS), (
        "shared validator model set changed; update SHARED_MODELS + golden"
    )
    mismatched = [name for name in SHARED_MODELS if actual[name] != golden[name]]
    assert not mismatched, (
        f"validator wire model(s) {mismatched} drifted from the platform "
        f"contract. If intended, regenerate ditto/tests/contract/"
        f"validator_contract.json from ditto-platform via "
        f"scripts/gen_validator_contract.py and commit it with the change."
    )


def test_public_agent_status_matches_platform_generated_contract() -> None:
    """Keep the shared lifecycle enum aligned with the platform contract."""
    golden = json.loads(_GOLDEN.read_text())
    definitions = {
        tuple(schema["$defs"]["AgentStatus"]["enum"])
        for schema in golden.values()
        if "AgentStatus" in schema.get("$defs", {})
    }
    assert definitions == {tuple(status.value for status in AgentStatus)}
