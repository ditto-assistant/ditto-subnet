"""Validator-only shadow coding artifact delivery contract."""

from __future__ import annotations

import ipaddress
import json
import posixpath
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ditto.api_models.coding_certification import ContentAddressedKey
from ditto.api_models.coding_evaluation import (
    CodingEvaluationModel,
    OpaqueId,
    Sha256,
)
from ditto.api_models.coding_selection import (
    CodingCatalogBudgets,
    CodingCatalogGraderPlan,
    CodingCatalogIssue,
    CodingCatalogResourceProfile,
    CodingCatalogRunnerPlan,
    CodingCatalogRuntimePolicy,
    CodingSelectionRunManifest,
    coding_catalog_budgets_digest,
    coding_catalog_grader_plan_digest,
    coding_catalog_issue_digest,
    coding_catalog_resource_profile_digest,
    coding_catalog_runner_plan_digest,
    coding_catalog_runtime_policy_digest,
    coding_selection_run_manifest_digest,
    validate_coding_catalog_grading_bundle,
)

_MAX_JSON_BYTES = 32 << 10
_MAX_SIGNED_URL_BYTES = 16 << 10
_MAX_CAPABILITY_SECONDS = 900
_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_PATTERN = r"^[0-9a-fA-F]{128}$"
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)


class CodingArtifactKind(StrEnum):
    VISIBLE_BUNDLE = "visible-bundle"
    MEMORY_BUNDLE = "memory-bundle"
    RESOURCE_PROFILE = "resource-profile"
    GRADER_BUNDLE = "grader-bundle"


class CodingArtifactAudience(StrEnum):
    WORKSPACE_MATERIALIZER = "workspace-materializer"
    MEMORY_SEED_PROJECTOR = "memory-seed-projector"
    RESOURCE_SUPERVISOR = "resource-supervisor"
    PROTECTED_GRADER = "protected-grader"


class CodingArtifactDeliveryPhase(StrEnum):
    AUTHORING = "authoring"
    GRADING = "grading"


CODING_ARTIFACT_MAX_BYTES = MappingProxyType(
    {
        CodingArtifactKind.VISIBLE_BUNDLE: 2 << 30,
        CodingArtifactKind.MEMORY_BUNDLE: 4 << 20,
        CodingArtifactKind.RESOURCE_PROFILE: 4 << 20,
        CodingArtifactKind.GRADER_BUNDLE: 512 << 20,
    }
)

CODING_ARTIFACT_AUDIENCE = MappingProxyType(
    {
        CodingArtifactKind.VISIBLE_BUNDLE: (
            CodingArtifactAudience.WORKSPACE_MATERIALIZER
        ),
        CodingArtifactKind.MEMORY_BUNDLE: CodingArtifactAudience.MEMORY_SEED_PROJECTOR,
        CodingArtifactKind.RESOURCE_PROFILE: CodingArtifactAudience.RESOURCE_SUPERVISOR,
        CodingArtifactKind.GRADER_BUNDLE: CodingArtifactAudience.PROTECTED_GRADER,
    }
)

CODING_ARTIFACT_PHASES = MappingProxyType(
    {
        CodingArtifactKind.VISIBLE_BUNDLE: frozenset(
            {
                CodingArtifactDeliveryPhase.AUTHORING,
                CodingArtifactDeliveryPhase.GRADING,
            }
        ),
        CodingArtifactKind.MEMORY_BUNDLE: frozenset(
            {CodingArtifactDeliveryPhase.AUTHORING}
        ),
        CodingArtifactKind.RESOURCE_PROFILE: frozenset(
            {
                CodingArtifactDeliveryPhase.AUTHORING,
                CodingArtifactDeliveryPhase.GRADING,
            }
        ),
        CodingArtifactKind.GRADER_BUNDLE: frozenset(
            {CodingArtifactDeliveryPhase.GRADING}
        ),
    }
)


class CodingArtifactCapabilityEnvelope(CodingEvaluationModel):
    """One audience- and phase-projected bearer capability."""

    schema_name: Literal["dittobench-coding-artifact-capability-v1"] = Field(
        alias="schema"
    )
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    ticket_id: UUID
    ticket_deadline: datetime
    delivery_phase: CodingArtifactDeliveryPhase
    artifact_kind: CodingArtifactKind
    audience: CodingArtifactAudience
    sha256: Sha256
    size_bytes: Annotated[int, Field(strict=True, ge=1)]
    url: Annotated[
        str, Field(min_length=1, max_length=_MAX_SIGNED_URL_BYTES, repr=False)
    ]
    expires_at: datetime

    @field_validator("coding_contract_version", mode="before")
    @classmethod
    def contract_version_is_strict_integer(cls, value: Any) -> Any:
        if type(value) is not int:  # bool is an int subclass
            raise ValueError("coding contract version must be an integer")
        return value

    @field_validator("weight_eligible", mode="before")
    @classmethod
    def weight_eligibility_is_strict_boolean(cls, value: Any) -> Any:
        if type(value) is not bool:
            raise ValueError("weight eligibility must be a boolean")
        return value

    @field_validator("ticket_deadline", "expires_at", mode="before")
    @classmethod
    def timestamp_input_is_strict(cls, value: Any) -> Any:
        if isinstance(value, str):
            if _RFC3339.fullmatch(value) is None:
                raise ValueError("coding artifact timestamps must use RFC3339")
        elif not isinstance(value, datetime):
            raise ValueError("coding artifact timestamps must be strings or datetimes")
        return value

    @field_validator("ticket_deadline", "expires_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding artifact timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def capability_is_coherent(self) -> CodingArtifactCapabilityEnvelope:
        if self.ticket_id.int == 0:
            raise ValueError("coding artifact ticket ID must be nonzero")
        if self.expires_at.microsecond != 0:
            raise ValueError("coding artifact expiry must have whole-second precision")
        if self.expires_at > self.ticket_deadline:
            raise ValueError("coding artifact URL outlives its ticket")
        if self.size_bytes > CODING_ARTIFACT_MAX_BYTES[self.artifact_kind]:
            raise ValueError("coding artifact size exceeds its kind bound")
        if self.audience is not CODING_ARTIFACT_AUDIENCE[self.artifact_kind]:
            raise ValueError("coding artifact audience disagrees with its kind")
        if self.delivery_phase not in CODING_ARTIFACT_PHASES[self.artifact_kind]:
            raise ValueError("coding artifact kind is forbidden in this delivery phase")
        _validate_signed_url(self)
        return self


class CodingAuthoringLeaseRequest(BaseModel):
    """Signed request for one validator-owned shadow authoring lease."""

    model_config = ConfigDict(extra="ignore")

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    ticket_id: UUID
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIGNATURE_PATTERN)]

    @field_validator("requested_at")
    @classmethod
    def requested_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "coding authoring request timestamp must be timezone-aware"
            )
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def identifiers_are_nonzero(self) -> CodingAuthoringLeaseRequest:
        if self.ticket_id.int == 0 or self.nonce.int == 0:
            raise ValueError("coding authoring request UUIDs must be nonzero")
        return self


class CodingAuthoringLeaseResponse(CodingEvaluationModel):
    """One selected task and exactly three authoring artifact capabilities."""

    schema_name: Literal["dittobench-coding-authoring-lease-v1"] = Field(alias="schema")
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    ticket_id: UUID
    ticket_deadline: datetime
    coding_run_id: OpaqueId
    run_manifest_sha256: Sha256
    task_set_manifest_sha256: Sha256
    repository_epoch: OpaqueId
    issue_sha256: Sha256
    runtime_policy_sha256: Sha256
    budgets_sha256: Sha256
    issue: CodingCatalogIssue
    runtime_policy: CodingCatalogRuntimePolicy
    budgets: CodingCatalogBudgets
    runner_plan_sha256: Sha256
    runner_plan: CodingCatalogRunnerPlan
    run_manifest: CodingSelectionRunManifest
    capabilities: Annotated[
        list[CodingArtifactCapabilityEnvelope], Field(min_length=3, max_length=3)
    ]

    @field_validator("ticket_deadline")
    @classmethod
    def ticket_deadline_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding authoring ticket deadline must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def authoring_authority_is_coherent(self) -> CodingAuthoringLeaseResponse:
        if self.ticket_id.int == 0 or len(self.run_manifest.tasks) != 1:
            raise ValueError(
                "coding authoring lease requires one task and nonzero ticket"
            )
        if (
            self.run_manifest.coding_run_id != self.coding_run_id
            or self.run_manifest.task_set_manifest_sha256
            != self.task_set_manifest_sha256
            or coding_selection_run_manifest_digest(self.run_manifest)
            != self.run_manifest_sha256
            or coding_catalog_issue_digest(self.issue) != self.issue_sha256
            or coding_catalog_runtime_policy_digest(self.runtime_policy)
            != self.runtime_policy_sha256
            or coding_catalog_budgets_digest(self.budgets) != self.budgets_sha256
            or coding_catalog_runner_plan_digest(self.runner_plan)
            != self.runner_plan_sha256
        ):
            raise ValueError("coding authoring lease material disagrees with authority")
        expected_kinds = [
            CodingArtifactKind.VISIBLE_BUNDLE,
            CodingArtifactKind.MEMORY_BUNDLE,
            CodingArtifactKind.RESOURCE_PROFILE,
        ]
        if [
            capability.artifact_kind for capability in self.capabilities
        ] != expected_kinds:
            raise ValueError(
                "coding authoring capabilities are incomplete or unordered"
            )
        task = self.run_manifest.tasks[0]
        if (
            self.runner_plan.case_id != task.case_id
            or self.runner_plan.visible_bundle_sha256 != task.visible_bundle_sha256
            or self.runner_plan.base_tree_sha256 != task.base_tree_sha256
            or self.runtime_policy.editable_paths
            != sorted(
                self.runner_plan.editable_paths
                + self.runner_plan.creatable_paths
                + self.runner_plan.deletable_paths
            )
            or self.runtime_policy.test_command_ids
            != [command.id for command in self.runner_plan.test_commands]
            or self.runtime_policy.build_command_ids
            != [command.id for command in self.runner_plan.build_commands]
        ):
            raise ValueError("coding authoring runner plan disagrees with the lease")
        expected_digests = [
            task.visible_bundle_sha256,
            task.memory_bundle_sha256,
            task.resource_profile_sha256,
        ]
        if any(
            capability.ticket_id != self.ticket_id
            or capability.ticket_deadline != self.ticket_deadline
            or capability.delivery_phase is not CodingArtifactDeliveryPhase.AUTHORING
            or capability.sha256 != expected_digest
            for capability, expected_digest in zip(
                self.capabilities, expected_digests, strict=True
            )
        ):
            raise ValueError("coding authoring capabilities disagree with the lease")
        return self


class CodingGradingLeaseRequest(BaseModel):
    """Signed request for one freeze-gated shadow grading lease."""

    model_config = ConfigDict(extra="ignore")

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    agent_id: UUID
    run_row_id: UUID
    ticket_id: UUID
    freeze_id: UUID
    authoring_evidence_sha256: Sha256
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIGNATURE_PATTERN)]

    @field_validator("requested_at")
    @classmethod
    def requested_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding grading request timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def identifiers_are_nonzero(self) -> CodingGradingLeaseRequest:
        if any(
            value.int == 0
            for value in (
                self.agent_id,
                self.run_row_id,
                self.ticket_id,
                self.freeze_id,
                self.nonce,
            )
        ):
            raise ValueError("coding grading request UUIDs must be nonzero")
        return self


class CodingGradingLeaseResponse(CodingEvaluationModel):
    """One frozen task and exactly three grading artifact capabilities."""

    schema_name: Literal["dittobench-coding-grading-lease-v1"] = Field(alias="schema")
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    agent_id: UUID
    run_row_id: UUID
    ticket_id: UUID
    ticket_deadline: datetime
    coding_run_id: OpaqueId
    run_manifest_sha256: Sha256
    task_set_manifest_sha256: Sha256
    freeze_id: UUID
    authoring_evidence_sha256: Sha256
    frozen_patch_sha256: Sha256
    frozen_submission_object_key: ContentAddressedKey
    run_manifest: CodingSelectionRunManifest
    grader_plan: CodingCatalogGraderPlan
    grader_resource_profile: CodingCatalogResourceProfile
    capabilities: Annotated[
        list[CodingArtifactCapabilityEnvelope], Field(min_length=3, max_length=3)
    ]

    @field_validator("ticket_deadline")
    @classmethod
    def ticket_deadline_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding grading ticket deadline must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def grading_authority_is_coherent(self) -> CodingGradingLeaseResponse:
        if (
            any(
                value.int == 0
                for value in (
                    self.agent_id,
                    self.run_row_id,
                    self.ticket_id,
                    self.freeze_id,
                )
            )
            or len(self.run_manifest.tasks) != 1
        ):
            raise ValueError("coding grading lease identity is invalid")
        if (
            self.run_manifest.agent_id != str(self.agent_id)
            or self.run_manifest.coding_run_id != self.coding_run_id
            or self.run_manifest.task_set_manifest_sha256
            != self.task_set_manifest_sha256
            or coding_selection_run_manifest_digest(self.run_manifest)
            != self.run_manifest_sha256
            or self.frozen_submission_object_key != f"sha256/{self.frozen_patch_sha256}"
            or coding_catalog_grader_plan_digest(self.grader_plan)
            != self.run_manifest.tasks[0].grader_plan_sha256
            or coding_catalog_resource_profile_digest(self.grader_resource_profile)
            != self.run_manifest.tasks[0].resource_profile_sha256
        ):
            raise ValueError("coding grading lease material disagrees with authority")
        expected_kinds = [
            CodingArtifactKind.VISIBLE_BUNDLE,
            CodingArtifactKind.RESOURCE_PROFILE,
            CodingArtifactKind.GRADER_BUNDLE,
        ]
        if [item.artifact_kind for item in self.capabilities] != expected_kinds:
            raise ValueError("coding grading capabilities are incomplete or unordered")
        task = self.run_manifest.tasks[0]
        if (
            self.grader_plan.case_id != task.case_id
            or self.grader_plan.variant_id != task.variant_id
            or self.grader_plan.visible_bundle_sha256 != task.visible_bundle_sha256
            or self.grader_plan.base_tree_sha256 != task.base_tree_sha256
            or self.grader_plan.grader_bundle_sha256 != task.grader_bundle_sha256
            or self.grader_plan.grader_image_digest != task.grader_image_digest
            or self.grader_plan.test_manifest_sha256 != task.test_manifest_sha256
        ):
            raise ValueError("coding grading plan disagrees with the selected task")
        validate_coding_catalog_grading_bundle(
            self.grader_plan, self.grader_resource_profile
        )
        expected_digests = [
            task.visible_bundle_sha256,
            task.resource_profile_sha256,
            task.grader_bundle_sha256,
        ]
        if any(
            capability.ticket_id != self.ticket_id
            or capability.ticket_deadline != self.ticket_deadline
            or capability.delivery_phase is not CodingArtifactDeliveryPhase.GRADING
            or capability.sha256 != digest
            for capability, digest in zip(
                self.capabilities, expected_digests, strict=True
            )
        ):
            raise ValueError("coding grading capabilities disagree with the lease")
        return self


def coding_authoring_lease_signing_message(
    *,
    validator_hotkey: str,
    ticket_id: UUID,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    """Bind one authoring request to a validator, ticket, nonce, and time."""

    if requested_at.tzinfo is None or requested_at.utcoffset() is None:
        raise ValueError("coding authoring request timestamp must be timezone-aware")
    timestamp = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    return "\x00".join(
        (
            "dittobench-coding-authoring-lease:v1",
            validator_hotkey,
            str(ticket_id),
            str(nonce),
            timestamp,
        )
    ).encode()


def coding_grading_lease_signing_message(
    *,
    validator_hotkey: str,
    agent_id: UUID,
    run_row_id: UUID,
    ticket_id: UUID,
    freeze_id: UUID,
    authoring_evidence_sha256: str,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    """Bind one grader release request to the immutable authoring freeze."""

    if requested_at.tzinfo is None or requested_at.utcoffset() is None:
        raise ValueError("coding grading request timestamp must be timezone-aware")
    timestamp = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    return "\x00".join(
        (
            "dittobench-coding-grading-lease:v1",
            validator_hotkey,
            str(agent_id),
            str(run_row_id),
            str(ticket_id),
            str(freeze_id),
            authoring_evidence_sha256,
            str(nonce),
            timestamp,
        )
    ).encode()


def parse_coding_artifact_capability_json(
    raw: str | bytes,
) -> CodingArtifactCapabilityEnvelope:
    """Decode one bounded document while rejecting duplicate fields."""

    if isinstance(raw, str):
        try:
            body = raw.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("coding artifact JSON is not valid UTF-8") from error
    else:
        body = raw
    if not 1 <= len(body) <= _MAX_JSON_BYTES:
        raise ValueError("coding artifact JSON size is outside bounds")
    try:
        decoded = json.loads(
            body,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except RecursionError as error:
        raise ValueError("coding artifact JSON nesting exceeds bounds") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("coding artifact JSON is invalid") from error
    _validate_json_tree(decoded, depth=0)
    if not isinstance(decoded, dict):
        raise ValueError("coding artifact JSON must be an object")
    try:
        return CodingArtifactCapabilityEnvelope.model_validate(decoded)
    except ValidationError:
        raise ValueError(
            "coding artifact capability known fields are invalid"
        ) from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"coding artifact JSON repeats field {key!r}")
        output[key] = value
    return output


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant {value}")


def _validate_json_tree(value: Any, *, depth: int) -> None:
    if depth > 32:
        raise ValueError("coding artifact JSON nesting exceeds 32 levels")
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("coding artifact JSON contains an unpaired surrogate")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_json_tree(key, depth=depth + 1)
            _validate_json_tree(item, depth=depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_tree(item, depth=depth + 1)


def _validate_signed_url(capability: CodingArtifactCapabilityEnvelope) -> None:
    value = capability.url
    if (
        len(value.encode("utf-8")) > _MAX_SIGNED_URL_BYTES
        or not value.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("coding artifact URL is outside bounds")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("coding artifact URL is invalid") from error
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.query
        or ";" in parsed.query
        or not _valid_percent_encoding(parsed.query)
        or (port is not None and not 1 <= port <= 65_535)
        or "%" in parsed.path
        or "//" in parsed.path
        or posixpath.normpath(parsed.path) != parsed.path
    ):
        raise ValueError("coding artifact URL is invalid")
    if parsed.scheme == "http" and not _loopback(hostname):
        raise ValueError("coding artifact URL requires HTTPS outside loopback")
    expected = (
        f"/coding-artifacts/v1/{capability.artifact_kind.value}"
        f"/sha256/{capability.sha256}"
    )
    if not parsed.path.endswith(expected):
        raise ValueError("coding artifact URL path disagrees with known fields")
    query: dict[str, list[str]] = {}
    for name, values in parse_qs(parsed.query).items():
        query.setdefault(name.lower(), []).extend(values)
    if sum(len(values) for values in query.values()) > 64:
        raise ValueError("coding artifact URL has too many query fields")
    if _signed_expiry(query) != capability.expires_at:
        raise ValueError("coding artifact signed expiry disagrees with known fields")


def _signed_expiry(query: dict[str, list[str]]) -> datetime:
    v4 = query.get("x-amz-signature", [])
    v2 = query.get("signature", [])
    if bool(v4) == bool(v2):
        raise ValueError("coding artifact signature fields are ambiguous")
    if v4:
        dates = query.get("x-amz-date", [])
        durations = query.get("x-amz-expires", [])
        if len(v4) != 1 or len(dates) != 1 or len(durations) != 1:
            raise ValueError("coding artifact v4 signature fields are invalid")
        try:
            signed_at = datetime.strptime(dates[0], "%Y%m%dT%H%M%SZ").replace(
                tzinfo=UTC
            )
            duration = _parse_decimal(durations[0])
        except ValueError as error:
            raise ValueError("coding artifact v4 expiry is invalid") from error
        if not 60 <= duration <= _MAX_CAPABILITY_SECONDS:
            raise ValueError("coding artifact v4 expiry is outside bounds")
        return signed_at + timedelta(seconds=duration)
    expires = query.get("expires", [])
    if len(v2) != 1 or len(expires) != 1:
        raise ValueError("coding artifact v2 signature fields are invalid")
    try:
        return datetime.fromtimestamp(_parse_decimal(expires[0]), tz=UTC)
    except (OverflowError, ValueError) as error:
        raise ValueError("coding artifact v2 expiry is invalid") from error


def _loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _valid_percent_encoding(value: str) -> bool:
    hex_digits = frozenset("0123456789abcdefABCDEF")
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if (
            index + 2 >= len(value)
            or value[index + 1] not in hex_digits
            or value[index + 2] not in hex_digits
        ):
            return False
        index += 3
    return True


def _parse_decimal(value: str) -> int:
    if not value or any(character not in "0123456789" for character in value):
        raise ValueError("coding artifact expiry must be ASCII decimal")
    return int(value)
