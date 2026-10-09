import json

import pytest
from pydantic import ValidationError

from ditto_screening_protocol.receipt_diagnostics import (
    ReceiptDiagnosticObservation,
    ReceiptDiagnosticReport,
    ReceiptValidationDiagnostic,
    diagnostic_signing_message,
)


def report(**updates):
    values = {
        "validator_hotkey": "validator",
        "netuid": 118,
        "timestamp": 123,
        "observation": ReceiptDiagnosticObservation(
            submission_status="not_attempted",
            recovery_status="validating_claim_failed",
            recovery_observed_at=122,
            page_deferred=1,
        ),
    }
    return ReceiptDiagnosticReport(**(values | updates))


def test_original_v1_signing_bytes_remain_frozen():
    expected = (
        b'ditto-receipt-diagnostics:v1:{"netuid":118,"observation":{"conflicts_dropped":0,'
        b'"page_deferred":1,"page_finalized":0,"page_forwarded":0,"page_receipts":0,'
        b'"recovery_observed_at":122,"recovery_status":"validating_claim_failed",'
        b'"submission_observed_at":null,"submission_status":"not_attempted"},'
        b'"schema_version":1,"timestamp":123,"validator_hotkey":"validator"}'
    )
    assert diagnostic_signing_message(report()) == expected


@pytest.mark.parametrize("version", [True, "1", 1.0])
def test_diagnostic_version_is_a_strict_integer(version):
    with pytest.raises(ValidationError):
        report(schema_version=version)


def test_only_v2_authenticates_validation_codes_and_unknown_fields_are_ignored():
    raw = report().model_dump(mode="json")
    raw["observation"]["last_validation"] = {
        "error_count": 1,
        "fields": ["root:value_error:vector_digest"],
    }
    with pytest.raises(ValidationError):
        ReceiptDiagnosticReport.model_validate(raw)
    raw["schema_version"] = 2
    validated = ReceiptDiagnosticReport.model_validate(raw)
    message = diagnostic_signing_message(validated)
    assert message.startswith(b"ditto-receipt-diagnostics:v2:")
    assert (
        json.loads(message.split(b":", 2)[2])["observation"]["last_validation"]
        == raw["observation"]["last_validation"]
    )
    raw["future"] = "ignored"
    raw["observation"]["future"] = "ignored"
    raw["observation"]["last_validation"]["future"] = "ignored"
    assert (
        diagnostic_signing_message(ReceiptDiagnosticReport.model_validate(raw))
        == message
    )


def test_schema_owned_field_names_can_contain_digits():
    diagnostic = ReceiptValidationDiagnostic(
        error_count=1,
        fields=["provenance.champion_artifact_sha256:string_pattern_mismatch"],
    )
    message = diagnostic_signing_message(
        report(
            schema_version=2,
            observation=report().observation.model_copy(
                update={"last_validation": diagnostic}
            ),
        )
    )
    assert b"champion_artifact_sha256:string_pattern_mismatch" in message


@pytest.mark.parametrize(
    "update",
    [
        {"error_count": True},
        {"error_count": -1},
        {"fields": ["root:value_error"] * 6},
        {"fields": ["root:value_error:raw message"]},
        {"fields": ["root:value_error:rule123"]},
        {"fields": ["https://private.example/receipt"]},
        {"fields": [123]},
        {"fields": ["a" * 513 + ":missing"]},
    ],
)
def test_validation_codes_are_strict_and_bounded(update):
    with pytest.raises(ValidationError):
        ReceiptValidationDiagnostic.model_validate(
            {"error_count": 1, "fields": ["root:value_error"]} | update
        )
