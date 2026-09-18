"""Authenticated, bounded relay observations; never emission evidence."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Counter = Annotated[int, Field(ge=0, le=2147483647, strict=True)]
Timestamp = Annotated[int, Field(ge=0, strict=True)]


class ReceiptDiagnosticObservation(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    submission_status: Literal[
        "not_attempted",
        "missing_provenance_or_transport",
        "invalid_provenance",
        "submitting_pylon",
        "unsupported",
        "accepted",
        "uncertain",
    ]
    submission_observed_at: Timestamp | None = None
    recovery_status: Literal[
        "not_attempted",
        "unsupported",
        "reading_pylon",
        "validating_claim",
        "forwarded",
        "page_complete",
        "reading_pylon_failed",
        "validating_claim_failed",
        "forwarding_platform_failed",
        "acknowledging_pylon_failed",
        "validating_page_failed",
    ]
    recovery_observed_at: Timestamp | None = None
    page_receipts: Counter = 0
    page_finalized: Counter = 0
    page_forwarded: Counter = 0
    page_deferred: Counter = 0


class ReceiptDiagnosticReport(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    schema_version: Literal[1] = 1
    validator_hotkey: Annotated[str, Field(min_length=1, max_length=128)]
    netuid: Annotated[int, Field(ge=0, le=65535, strict=True)]
    timestamp: Timestamp
    observation: ReceiptDiagnosticObservation


def diagnostic_signing_message(report: ReceiptDiagnosticReport) -> bytes:
    return (
        b"ditto-receipt-diagnostics:v1:"
        + json.dumps(
            report.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    )


class SubmitReceiptDiagnostics(BaseModel):
    model_config = ConfigDict(extra="ignore")
    report: ReceiptDiagnosticReport
    signature: Annotated[str, Field(pattern=r"^(0x)?[0-9a-fA-F]{128}$")]
