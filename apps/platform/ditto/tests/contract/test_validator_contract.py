"""Guard: the validator wire models keep a stable structural contract.

The platform's ``ditto/api_models/validator.py`` is the **source of truth** for
the validator wire contract (the OpenAPI schema is the contract; there is no
shared package with ``ditto-subnet``). This test pins the structural shape of
the shared models to the committed golden so an accidental field rename/retype/
add/remove is caught here; the validator client in ditto-subnet holds a copy of
the same golden and asserts its models match it.

On an intentional contract change, regenerate the golden with the
repository-root ``scripts/gen_validator_contract.py`` -- the only generator --
and commit it with the model edit::

    cd apps/platform && uv run python ../../scripts/gen_validator_contract.py

One run writes this golden and the byte-identical ditto-subnet copy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import ditto.api_models.validator as validator_models
import ditto_screening_protocol.bench_v9 as shared_bench_v9
import ditto_screening_protocol.confirmation as shared_confirmation
import ditto_screening_protocol.confirmation_transport as shared_confirmation_transport
from ditto.api_models import confirmation_bundles, validator_confirmation
from ditto.api_models.agent_status import AgentStatus
from ditto.tests.contract._schema import (
    CONFIRMATION_MODELS,
    SHARED_MODELS,
    compute_confirmation_contract,
    compute_contract,
)

# ``scripts/`` is not an importable package; the freshness helper is shared
# with the generators rather than duplicated into each contract test copy.
sys.path.insert(0, str(Path(__file__).resolve().parents[5] / "scripts"))

from screening_protocol_freshness import hint as stale_install_hint  # noqa: E402

_GOLDEN = Path(__file__).parent / "validator_contract.json"
_CONFIRMATION_GOLDEN = Path(__file__).parent / "confirmation_contract.json"
_VALIDATOR_CONTRACT_DIR = Path(__file__).resolve().parents[5] / "ditto/tests/contract"

_SHARED_BENCH_V9_MODELS = (
    "V9ScoreContract",
    "V9ThresholdProfile",
    "V9GateExclusions",
    "V9ModelUseGate",
    "V9AuthoritativeToolGate",
    "V9ScoreGateEvidence",
    "V9BaseEvidence",
)


def test_v9_gate_models_are_imported_from_canonical_shared_package() -> None:
    for name in _SHARED_BENCH_V9_MODELS:
        assert getattr(validator_models, name) is getattr(shared_bench_v9, name)


def test_v9_confirmation_evidence_models_are_shared_without_schema_drift() -> None:
    common = (
        "AblationBudget",
        "AblationDimensionEnvelope",
        "AblationEvidence",
        "AblationSyntheticUsage",
        "ConfirmationCompletionReport",
        "ConfirmationCompositePolicy",
        "ConfirmationEvidenceRoot",
        "ConfirmationUsageTotals",
        "LongMemCapabilityScore",
        "LongMemDimensionEnvelope",
        "LongMemEvidence",
        "LongMemProviderLaneEvidence",
        "LongMemScoreEvidence",
    )
    for name in common:
        assert getattr(confirmation_bundles, name) is getattr(shared_confirmation, name)
    assert validator_models.V9ConfirmationCompositePolicy is (
        shared_confirmation.V9ConfirmationCompositePolicy
    )
    assert validator_models.V9ConfirmationEvidenceRoot is (
        shared_confirmation.V9ConfirmationEvidenceRoot
    )


def test_v9_confirmation_transport_executes_only_shared_model_classes() -> None:
    common = (
        "ConfirmationAblationCoordinatorProfile",
        "ConfirmationAblationProfile",
        "ConfirmationCompositeProfile",
        "ConfirmationExecutionProfile",
        "ConfirmationProviderLaneProfile",
        "V9ConfirmationClaimRequest",
        "V9ConfirmationFailRequest",
        "V9ConfirmationFailResponse",
        "V9ConfirmationJobResponse",
        "V9ConfirmationPrepareRequest",
        "V9ConfirmationPreparedReport",
        "V9ConfirmationRawDimension",
        "V9ConfirmationSubmitRequest",
        "V9ConfirmationSubmitResponse",
    )
    for name in common:
        assert getattr(validator_confirmation, name) is getattr(
            shared_confirmation_transport, name
        )

    assert confirmation_bundles.ConfirmationBundleMode is (
        shared_confirmation_transport.ConfirmationBundleMode
    )
    local_execution = validator_confirmation.ConfirmationExecutionProfile
    shared_execution = shared_confirmation_transport.ConfirmationExecutionProfile
    local_roles = local_execution.ablation_roles_are_not_swappable
    shared_roles = shared_execution.ablation_roles_are_not_swappable
    assert local_roles is shared_roles
    local_failure = validator_confirmation.V9ConfirmationFailRequest
    shared_failure = shared_confirmation_transport.V9ConfirmationFailRequest
    local_timezone = local_failure.requested_at_must_be_timezone_aware.__func__
    shared_timezone = shared_failure.requested_at_must_be_timezone_aware.__func__
    assert local_timezone is shared_timezone


def test_monorepo_validator_goldens_match_validator_byte_for_byte() -> None:
    for filename in ("validator_contract.json", "confirmation_contract.json"):
        local = (Path(__file__).parent / filename).read_bytes()
        validator = (_VALIDATOR_CONTRACT_DIR / filename).read_bytes()
        assert local == validator, (
            f"{filename} diverged inside the monorepo; regenerate once from "
            "Platform and commit the same artifact to both contract directories"
            f"{stale_install_hint()}"
        )


def test_validator_models_match_committed_contract() -> None:
    golden = json.loads(_GOLDEN.read_text())
    actual = compute_contract()

    assert set(actual) == set(golden) == set(SHARED_MODELS), (
        "shared validator model set changed; update SHARED_MODELS + golden"
    )
    mismatched = [name for name in SHARED_MODELS if actual[name] != golden[name]]
    assert not mismatched, (
        f"validator wire model(s) {mismatched} drifted from the committed "
        f"contract. If intended, regenerate this golden with "
        f"`uv run python ../../scripts/gen_validator_contract.py` -- one run "
        f"also refreshes the byte-identical ditto-subnet copy -- and commit "
        f"both with the change.{stale_install_hint()}"
    )


def test_v9_confirmation_models_match_committed_contract() -> None:
    golden = json.loads(_CONFIRMATION_GOLDEN.read_text())
    actual = compute_confirmation_contract()
    assert set(actual) == set(golden) == set(CONFIRMATION_MODELS)
    mismatched = [name for name in CONFIRMATION_MODELS if actual[name] != golden[name]]
    assert not mismatched, (
        f"v9 confirmation wire model(s) {mismatched} drifted from the committed "
        "contract; regenerate confirmation_contract.json"
        f"{stale_install_hint()}"
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
