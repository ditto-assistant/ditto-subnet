"""Guard: the validator wire models keep a stable structural contract.

The platform's ``ditto/api_models/validator.py`` is the **source of truth** for
the validator wire contract (the OpenAPI schema is the contract; there is no
shared package with ``ditto-subnet``). This test pins the structural shape of
the shared models to the committed golden so an accidental field rename/retype/
add/remove is caught here; the validator client in ditto-subnet holds a copy of
the same golden and asserts its models match it.

On an intentional contract change, regenerate the golden with
``scripts/gen_validator_contract.py`` and commit it with the model edit.
"""

from __future__ import annotations

import json
from pathlib import Path

from ditto.api_models.agent_status import AgentStatus
from ditto.tests.contract._schema import SHARED_MODELS, compute_contract

_GOLDEN = Path(__file__).parent / "validator_contract.json"


def test_validator_models_match_committed_contract() -> None:
    golden = json.loads(_GOLDEN.read_text())
    actual = compute_contract()

    assert set(actual) == set(golden) == set(SHARED_MODELS), (
        "shared validator model set changed; update SHARED_MODELS + golden"
    )
    mismatched = [name for name in SHARED_MODELS if actual[name] != golden[name]]
    assert not mismatched, (
        f"validator wire model(s) {mismatched} drifted from the committed "
        f"contract. If intended, regenerate ditto/tests/contract/"
        f"validator_contract.json via scripts/gen_validator_contract.py and "
        f"commit it with the change (and sync ditto-subnet's copy)."
    )


def test_ledger_entry_exposes_append_only_confirmation_history() -> None:
    """The top-5 lane's ledger exposure is present and shaped as a record list."""
    contract = compute_contract()
    prop = contract["LedgerEntry"]["properties"]["confirmation_history"]
    record = contract["LedgerEntry"]["$defs"]["ConfirmationScoreRecord"]
    assert set(record["properties"]) == {
        "seed",
        "composite",
        "validator_hotkey",
        "bench_version",
        "signature",
    }
    # Optional list of records (absent -> fold falls back to legacy arrays).
    assert prop["default"] is None
    ref_holder = next(item for item in prop["anyOf"] if item.get("type") == "array")
    assert ref_holder["items"]["$ref"].endswith("/ConfirmationScoreRecord")


def test_agent_status_enum_matches_contract() -> None:
    golden = json.loads(_GOLDEN.read_text())
    definitions = {
        tuple(schema["$defs"]["AgentStatus"]["enum"])
        for schema in golden.values()
        if "AgentStatus" in schema.get("$defs", {})
    }
    assert definitions == {tuple(status.value for status in AgentStatus)}
