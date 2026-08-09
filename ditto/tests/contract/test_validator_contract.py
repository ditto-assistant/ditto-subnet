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

import ditto.api_models.validator as validator_models
import ditto_screening_protocol.bench_v9 as shared_bench_v9
import ditto_screening_protocol.confirmation as shared_confirmation
import ditto_screening_protocol.confirmation_transport as shared_confirmation_transport
from ditto.api_models import validator_confirmation
from ditto.api_models.agent_status import AgentStatus
from ditto.tests.contract._schema import (
    CONFIRMATION_MODELS,
    SHARED_MODELS,
    compute_confirmation_contract,
    compute_contract,
    compute_miner_contract,
)

_GOLDEN = Path(__file__).parent / "validator_contract.json"
_MINER_GOLDEN = Path(__file__).parent / "miner_contract.json"
_CONFIRMATION_GOLDEN = Path(__file__).parent / "confirmation_contract.json"
_PLATFORM_CONTRACT_DIR = (
    Path(__file__).resolve().parents[3] / "apps/platform/ditto/tests/contract"
)

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
        "ConfirmationUsageTotals",
        "LongMemCapabilityScore",
        "LongMemDimensionEnvelope",
        "LongMemEvidence",
        "LongMemProviderLaneEvidence",
        "LongMemScoreEvidence",
    )
    for name in common:
        assert getattr(validator_confirmation, name) is getattr(
            shared_confirmation, name
        )
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
        "ConfirmationBundleMode",
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

    local_execution = validator_confirmation.ConfirmationExecutionProfile
    shared_execution = shared_confirmation_transport.ConfirmationExecutionProfile
    local_roles = local_execution.ablation_roles_are_not_swappable
    shared_roles = shared_execution.ablation_roles_are_not_swappable
    assert local_roles is shared_roles
    local_claim = validator_confirmation.V9ConfirmationClaimRequest
    shared_claim = shared_confirmation_transport.V9ConfirmationClaimRequest
    local_timezone = local_claim.requested_at_must_be_timezone_aware.__func__
    shared_timezone = shared_claim.requested_at_must_be_timezone_aware.__func__
    assert local_timezone is shared_timezone


def test_monorepo_validator_goldens_match_platform_byte_for_byte() -> None:
    for filename in ("validator_contract.json", "confirmation_contract.json"):
        local = (Path(__file__).parent / filename).read_bytes()
        platform = (_PLATFORM_CONTRACT_DIR / filename).read_bytes()
        assert local == platform, (
            f"{filename} diverged inside the monorepo; regenerate once from "
            "Platform and commit the same artifact to both contract directories"
        )


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


def test_v9_confirmation_models_match_platform_contract() -> None:
    golden = json.loads(_CONFIRMATION_GOLDEN.read_text())
    actual = compute_confirmation_contract()
    assert set(actual) == set(golden) == set(CONFIRMATION_MODELS)
    mismatched = [name for name in CONFIRMATION_MODELS if actual[name] != golden[name]]
    assert not mismatched, (
        f"v9 confirmation wire model(s) {mismatched} drifted from the Platform "
        "contract; regenerate confirmation_contract.json from Platform"
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


def test_miner_models_match_platform_contract() -> None:
    """Guard: the miner CLI's wire models match the platform's contract.

    The validator golden above has always existed; these models had no guard,
    and they drifted. The platform's payment path grew five fields the CLI
    never learned to read, so ``extra='ignore'`` discarded a "your payment was
    banked as a credit, not spent" answer and the CLI printed an ordinary
    success over it.
    """
    golden = json.loads(_MINER_GOLDEN.read_text())
    actual = compute_miner_contract()

    assert set(actual) == set(golden), (
        "miner wire model set changed; update MINER_MODELS + regenerate the "
        "golden from ditto-platform"
    )
    mismatched = [name for name in sorted(golden) if actual[name] != golden[name]]
    assert not mismatched, (
        f"miner wire model(s) {mismatched} drifted from the platform contract. "
        f"If intended, regenerate ditto/tests/contract/miner_contract.json from "
        f"ditto-platform via scripts/gen_validator_contract.py and commit it "
        f"with the change."
    )
