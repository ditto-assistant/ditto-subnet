"""Admin wire models for the shadow coding-certification canary controls.

The allowlist is an append-only, strict operator restriction on who may run the
contract-v1 certification path: with no revision, a refuse-all revision, or a
corrupt revision, Platform refuses every lease, grant, harness, and receipt.
Only exact listed tuples are admitted. Lease rows are a read-only audit view.
Neither participates in scoring, weights, or emissions.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from ditto.api_models.coding_certification import CodingCertificationStatus
from ditto.api_models.coding_certification_leases import (
    CodingCertificationLeaseStatus,
)

_SHA256 = r"^[0-9a-f]{64}$"
_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
CODING_CERTIFICATION_ALLOWLIST_SCHEMA = "ditto-coding-certification-allowlist-v1"
CODING_CERTIFICATION_ALLOWLIST_MAX_ENTRIES = 16

CodingCertificationAllowlistKey = tuple[str, str, str, str]
"""``(agent_id, artifact_sha256, screened_image_sha256, validator_hotkey)``."""


class CodingCertificationAllowlistEntry(BaseModel):
    """One exact certifiable tuple. Every field must match; nothing is a wildcard."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    artifact_sha256: Annotated[str, Field(pattern=_SHA256)]
    screened_image_sha256: Annotated[str, Field(pattern=_SHA256)]
    """The agent's verified screened-image archive digest; a rebuild never matches."""
    validator_hotkey: Annotated[str, Field(pattern=_SS58)]

    @field_validator("agent_id")
    @classmethod
    def agent_is_nonzero(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("coding certification allowlist agent_id is nil")
        return value

    def key(self) -> CodingCertificationAllowlistKey:
        return (
            str(self.agent_id),
            self.artifact_sha256,
            self.screened_image_sha256,
            self.validator_hotkey,
        )


def canonical_coding_certification_allowlist_entries(
    entries: list[CodingCertificationAllowlistEntry],
) -> list[CodingCertificationAllowlistEntry]:
    return sorted(entries, key=lambda entry: entry.key())


def coding_certification_allowlist_entry_json(
    entry: CodingCertificationAllowlistEntry,
) -> dict[str, str]:
    """The stored and checksummed form of one exact tuple."""

    agent_id, artifact_sha256, screened_image_sha256, validator_hotkey = entry.key()
    return {
        "agent_id": agent_id,
        "artifact_sha256": artifact_sha256,
        "screened_image_sha256": screened_image_sha256,
        "validator_hotkey": validator_hotkey,
    }


def coding_certification_allowlist_checksum(
    *,
    enabled: bool,
    entries: list[CodingCertificationAllowlistEntry],
) -> str:
    """Digest of the exact restriction, independent of submitted entry order."""

    body = json.dumps(
        {
            "schema": CODING_CERTIFICATION_ALLOWLIST_SCHEMA,
            "enabled": enabled,
            "entries": [
                coding_certification_allowlist_entry_json(entry)
                for entry in canonical_coding_certification_allowlist_entries(entries)
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def coding_certification_allowlist_confirmation(
    *, enabled: bool, entry_count: int
) -> str:
    if not enabled:
        return "APPLY CODING CERTIFICATION ALLOWLIST REFUSE ALL"
    return f"APPLY CODING CERTIFICATION ALLOWLIST ENABLED {entry_count}"


AllowlistIntegrity = Literal["valid", "invalid"]
AllowlistEffect = Literal["refuse_all", "exact_tuples"]


class CodingCertificationAllowlistRevision(BaseModel):
    """One stored revision as enforcement sees it.

    ``enabled`` is the stored flag. ``integrity="invalid"`` means the stored
    entries failed to parse or their checksum does not bind them; such a
    revision reports no entries and ``effective="refuse_all"``.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    enabled: bool
    integrity: AllowlistIntegrity
    effective: AllowlistEffect
    entries: list[CodingCertificationAllowlistEntry]
    checksum: Annotated[str, Field(pattern=_SHA256)]
    reason: str
    actor: str
    created_at: datetime | None


class AdminCodingCertificationAllowlistResponse(BaseModel):
    """Current enforcement. ``enabled`` is true only for valid exact tuples."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    enabled: bool
    integrity: AllowlistIntegrity
    effective: AllowlistEffect
    current: CodingCertificationAllowlistRevision
    history: list[CodingCertificationAllowlistRevision]
    max_entries: int = CODING_CERTIFICATION_ALLOWLIST_MAX_ENTRIES
    weight_eligible: Literal[False] = False


class AdminCodingCertificationAllowlistApplyResponse(
    AdminCodingCertificationAllowlistResponse
):
    aborted_lease_count: Annotated[int, Field(ge=0)]
    revoked_inference_grant_count: Annotated[int, Field(ge=0)]


class AdminCodingCertificationAllowlistRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    expected_revision: Annotated[StrictInt, Field(ge=0)]
    enabled: StrictBool
    entries: Annotated[
        list[CodingCertificationAllowlistEntry],
        Field(max_length=CODING_CERTIFICATION_ALLOWLIST_MAX_ENTRIES),
    ]
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @field_validator("reason")
    @classmethod
    def reason_is_substantive(cls, value: str) -> str:
        if len(value.strip()) < 8:
            raise ValueError("reason must contain at least 8 non-whitespace characters")
        return value.strip()

    @field_validator("actor")
    @classmethod
    def actor_is_substantive(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("actor must not be blank")
        return value

    @model_validator(mode="after")
    def entries_are_exact(self) -> AdminCodingCertificationAllowlistRequest:
        if not self.enabled and self.entries:
            raise ValueError("a refuse-all allowlist must not carry entries")
        if self.enabled and not self.entries:
            raise ValueError(
                "an enabled allowlist needs at least one exact entry; "
                "use enabled=false to refuse all"
            )
        if len({entry.key() for entry in self.entries}) != len(self.entries):
            raise ValueError("coding certification allowlist entries must be unique")
        return self


class AdminCodingCertificationLeaseRecord(BaseModel):
    """One lease row. Grant ids, bearer digests, and image locators are omitted.

    ``completed`` means Platform accepted the lease's receipt; it is terminal
    and never expires. ``claim_allowlist_revision`` is the allowlist revision
    that admitted the claim, and ``aborted_allowlist_revision`` the revision
    whose write aborted the lease.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    lease_id: UUID
    agent_id: UUID
    artifact_sha256: str
    screened_image_sha256: str
    bench_version: int
    coding_contract_version: int
    validator_hotkey: str
    status: CodingCertificationLeaseStatus
    issued_at: datetime
    claimed_at: datetime | None
    aborted_at: datetime | None
    deadline: datetime
    deadline_passed: bool
    receipt_window_ends_at: datetime
    claim_allowlist_revision: int | None
    aborted_allowlist_revision: int | None
    inference_grant_status: Literal["pending", "active", "revoked", "exhausted"] | None
    receipt_status: CodingCertificationStatus | None
    weight_eligible: Literal[False] = False


class AdminCodingCertificationLeaseList(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    total: int
    limit: int
    offset: int
    leases: list[AdminCodingCertificationLeaseRecord]
    weight_eligible: Literal[False] = False
