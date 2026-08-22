"""Allowlisted prepare-report rejection codes stay closed and low-cardinality."""

from __future__ import annotations

from ditto.api_models.confirmation_bundles import PREPARE_REJECTION_CODES
from ditto.api_server.confirmation_evidence import ConfirmationEvidenceError
from ditto.api_server.confirmation_prepare_rejection import classify_prepare_rejection
from ditto.api_server.confirmation_wire import ConfirmationWireError


def test_known_prepare_failures_map_onto_the_closed_allowlist() -> None:
    cases = (
        (
            ConfirmationWireError("inference ablation Go evidence digest mismatch"),
            "go_evidence_digest_mismatch",
        ),
        (
            ConfirmationWireError(
                "inference ablation evidence fields drifted: "
                "missing=['selected_cases_sha256'], extra=[]"
            ),
            "go_evidence_fields_drifted",
        ),
        (
            ConfirmationWireError("unsupported Go ablation evidence status"),
            "unsupported_ablation_status",
        ),
        (
            ConfirmationWireError("unsupported Go ablation evidence contract"),
            "unsupported_ablation_contract",
        ),
        (
            ConfirmationEvidenceError("inference ablation profile drift"),
            "ablation_profile_drift",
        ),
        (
            ConfirmationEvidenceError("embedding ablation profile drift"),
            "ablation_profile_drift",
        ),
        (
            ConfirmationEvidenceError("inference ablation evidence digest mismatch"),
            "ablation_digest_mismatch",
        ),
        (
            ConfirmationEvidenceError("ablation affected-call count is not derived"),
            "ablation_accounting",
        ),
        (
            ConfirmationEvidenceError("LongMem evidence digest mismatch"),
            "longmem_digest_mismatch",
        ),
        (
            ConfirmationWireError("LongMem latency drift"),
            "longmem_latency_drift",
        ),
        (
            ConfirmationEvidenceError("LongMem profile checksum drift"),
            "longmem_profile_drift",
        ),
        (
            ConfirmationEvidenceError(
                "LongMem envelope accounting does not equal its provider lanes"
            ),
            "longmem_accounting",
        ),
        (
            ConfirmationWireError(
                "LongMem evidence targets an unsupported bench_version"
            ),
            "unsupported_bench_version",
        ),
        (
            ConfirmationWireError(
                "ablation coordinator latency must be a nonnegative integer"
            ),
            "confirmation_wire",
        ),
        (
            ConfirmationEvidenceError("base gates must be binary"),
            "confirmation_evidence",
        ),
        (RuntimeError("unexpected producer panic"), "unclassified"),
    )
    for error, code in cases:
        assert classify_prepare_rejection(error) == code
        assert code in PREPARE_REJECTION_CODES


def test_submitter_controlled_extra_keys_cannot_forge_another_code() -> None:
    forged = ConfirmationWireError(
        "inference ablation evidence fields drifted: missing=[], "
        "extra=['Go evidence digest mismatch']"
    )
    assert classify_prepare_rejection(forged) == "go_evidence_fields_drifted"
