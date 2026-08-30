"""Canonical evidence for the shadow coding-agent source integrity screen."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA256 = r"^[0-9a-f]{64}$"
_RULE = r"^[a-z][a-z0-9-]{0,63}$"
_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"


class CodingSourceScreenOutcome(StrEnum):
    PASS = "pass"
    DENY = "deny"
    QUARANTINE = "quarantine"
    ADVISORY = "advisory"
    INFRASTRUCTURE = "infrastructure"


class CodingSourceScreenSeverity(StrEnum):
    DENY = "deny"
    QUARANTINE = "quarantine"
    ADVISORY = "advisory"


class CodingSourceScreenFinding(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    rule_id: Annotated[str, Field(pattern=_RULE)]
    severity: CodingSourceScreenSeverity
    evidence_sha256: Annotated[str, Field(pattern=_SHA256)]
    path_sha256: Annotated[str | None, Field(default=None, pattern=_SHA256)]
    line_start: Annotated[int | None, Field(default=None, ge=1, le=10_000_000)]
    line_end: Annotated[int | None, Field(default=None, ge=1, le=10_000_000)]

    @model_validator(mode="after")
    def line_range_is_coherent(self) -> CodingSourceScreenFinding:
        if (self.line_start is None) != (self.line_end is None):
            raise ValueError("coding source finding line range is incomplete")
        if (
            self.line_start is not None
            and self.line_end is not None
            and self.line_end < self.line_start
        ):
            raise ValueError("coding source finding line range is invalid")
        return self


class CodingSourceScreenEvidence(BaseModel):
    """Content-addressed, source-safe coding screening result."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_name: Literal["dittobench-coding-source-screen-v1"] = Field(alias="schema")
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    agent_artifact_sha256: Annotated[str, Field(pattern=_SHA256)]
    screened_image_sha256: Annotated[str, Field(pattern=_SHA256)]
    analyzer_version: Annotated[str, Field(pattern=_RULE)]
    policy_version: Annotated[int, Field(ge=1, le=1_000_000)]
    outcome: CodingSourceScreenOutcome
    findings: Annotated[list[CodingSourceScreenFinding], Field(max_length=64)]
    evidence_sha256: Annotated[str, Field(pattern=_SHA256)]

    @model_validator(mode="after")
    def coherent(self) -> CodingSourceScreenEvidence:
        severities = {finding.severity for finding in self.findings}
        keys = [
            (finding.rule_id, finding.evidence_sha256, finding.path_sha256 or "")
            for finding in self.findings
        ]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("coding source findings are not canonical")
        if (
            self.outcome is CodingSourceScreenOutcome.DENY
            and CodingSourceScreenSeverity.DENY not in severities
        ):
            raise ValueError("coding source deny requires deterministic deny evidence")
        if (
            self.outcome is CodingSourceScreenOutcome.QUARANTINE
            and not {
                CodingSourceScreenSeverity.QUARANTINE,
                CodingSourceScreenSeverity.DENY,
            }
            & severities
        ):
            raise ValueError("coding source quarantine requires quarantine evidence")
        if self.outcome is CodingSourceScreenOutcome.PASS and severities:
            raise ValueError("coding source pass cannot carry findings")
        if self.outcome is CodingSourceScreenOutcome.ADVISORY and (
            CodingSourceScreenSeverity.DENY in severities
            or CodingSourceScreenSeverity.QUARANTINE in severities
        ):
            raise ValueError("coding source advisory result cannot carry deny evidence")
        if self.outcome is CodingSourceScreenOutcome.INFRASTRUCTURE and severities:
            raise ValueError(
                "coding source infrastructure result cannot blame the miner"
            )
        if self.evidence_sha256 != coding_source_screen_digest(self):
            raise ValueError("coding source screen evidence digest mismatch")
        return self


def coding_source_screen_digest(evidence: CodingSourceScreenEvidence) -> str:
    payload = evidence.model_dump(
        mode="json", by_alias=True, exclude={"evidence_sha256"}
    )
    body = (
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        + b"\n"
    )
    return hashlib.sha256(body).hexdigest()


def coding_source_screen_signing_message(
    *, screener_hotkey: str, evidence: CodingSourceScreenEvidence
) -> bytes:
    """Bind a screener signature to one canonical evidence result."""

    if not re.fullmatch(_SS58, screener_hotkey):
        raise ValueError("coding source screen signer is invalid")
    return "\x00".join(
        (
            "dittobench-coding-source-screen:v1",
            screener_hotkey,
            evidence.agent_artifact_sha256,
            evidence.screened_image_sha256,
            evidence.evidence_sha256,
        )
    ).encode()
