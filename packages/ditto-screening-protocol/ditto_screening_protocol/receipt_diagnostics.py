"""Authenticated, bounded relay observations; never emission evidence."""

from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Counter = Annotated[int, Field(ge=0, le=2147483647, strict=True)]
Timestamp = Annotated[int, Field(ge=0, strict=True)]


class ReceiptFailureContext(BaseModel):
    """Bounded unverified claim coordinates, never payout authority or payload."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    claimed_schema_version: Annotated[int, Field(ge=1, le=2, strict=True)] | None = None
    task_id: Annotated[int, Field(ge=1, le=2147483647, strict=True)] | None = None
    claimed_epoch_index: Counter | None = None
    claimed_commit_block: (
        Annotated[int, Field(ge=1, le=4294967295, strict=True)] | None
    ) = None
    attempt_id: UUID | None = None


class ReceiptValidationDiagnostic(BaseModel):
    """Last invalid claim's bounded codes, never the rejected claim's values."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    error_count: Counter
    fields: Annotated[
        list[
            Annotated[
                str,
                Field(
                    strict=True,
                    max_length=512,
                    pattern=r"^[a-z0-9_.*]+:[a-z_]+(:[a-z_]+)?$",
                ),
            ]
        ],
        Field(max_length=5),
    ]


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
        "conflict_dropped",
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
    conflicts_dropped: Counter = 0
    last_validation: ReceiptValidationDiagnostic | None = None
    failure_context: ReceiptFailureContext | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ReceiptDiagnosticReport(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    schema_version: Literal[1, 2, 3] = 1
    validator_hotkey: Annotated[str, Field(min_length=1, max_length=128)]
    netuid: Annotated[int, Field(ge=0, le=65535, strict=True)]
    timestamp: Timestamp
    observation: ReceiptDiagnosticObservation

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("diagnostic version must be an integer")
        return value

    @model_validator(mode="after")
    def validation_codes_require_v2(self) -> ReceiptDiagnosticReport:
        if self.schema_version == 1 and self.observation.last_validation is not None:
            raise ValueError("validation diagnostic requires schema version 2")
        if self.schema_version < 3 and self.observation.failure_context is not None:
            raise ValueError("receipt failure context requires schema version 3")
        return self


def diagnostic_signing_message(report: ReceiptDiagnosticReport) -> bytes:
    body = report.model_dump(mode="json")
    if report.schema_version < 3:
        body["observation"].pop("failure_context", None)
    if report.schema_version == 1:
        # Freeze the original signing bytes for existing validators. V1 rejects
        # non-null validation codes; only V2 can authenticate the new field.
        body["observation"].pop("last_validation")
    return (
        f"ditto-receipt-diagnostics:v{report.schema_version}:".encode()
        + json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    )


class SubmitReceiptDiagnostics(BaseModel):
    model_config = ConfigDict(extra="ignore")
    report: ReceiptDiagnosticReport
    signature: Annotated[str, Field(pattern=r"^(0x)?[0-9a-fA-F]{128}$")]
