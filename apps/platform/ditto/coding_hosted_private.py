"""Platform-private hosted selection and retrieval values, never wire responses."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal
from uuid import UUID

from ditto.api_models.coding_canonical import coding_canonical_sha256

AUTHORING_ROLES = (
    "catalog_record",
    "issue",
    "visible_bundle",
    "memory_bundle",
    "runtime_policy",
    "resource_profile",
)
GRADING_ROLES = (
    "catalog_record",
    "visible_bundle",
    "grader_bundle",
    "runtime_policy",
    "resource_profile",
)


@dataclass(frozen=True, repr=False)
class PrivateV2ObjectGrant:
    grant_id: UUID
    evaluation_id: UUID
    attempt_id: UUID
    registration_sha256: str
    catalog_index: int
    phase: Literal["authoring", "grading"]
    audience: Literal["platform-authoring", "platform-grading"]
    allowed_roles: tuple[str, ...]
    expires_at_unix: int
    frozen_patch_sha256: str | None


@dataclass(frozen=True, repr=False)
class HostedTaskSelection:
    """One isolated arm under a separate, private schedule commitment.

    This binds a selection; it does not sample, qualify or activate a release.
    A multi-arm evaluation needs distinct hosted assignments and attempts.
    """

    evaluation_id: UUID
    attempt_id: UUID
    registration_sha256: str
    artifact_sha256: str
    schedule_sha256: str
    catalog_index: int
    max_patch_bytes: int

    def projection(self) -> dict[str, Any]:
        raw = asdict(self)
        for name, value in raw.items():
            if name.endswith("_id"):
                if not isinstance(value, UUID) or value.int == 0:
                    raise ValueError("hosted selection identity is invalid")
                raw[name] = str(value)
            elif name.endswith("_sha256"):
                if not isinstance(value, str) or not re.fullmatch(
                    r"[0-9a-f]{64}", value
                ):
                    raise ValueError("hosted selection digest is invalid")
        if (
            type(self.catalog_index) is not int
            or not 0 <= self.catalog_index < 250
            or type(self.max_patch_bytes) is not int
            or not 1 <= self.max_patch_bytes <= 128 << 20
        ):
            raise ValueError("hosted selection bounds are invalid")
        return {
            "schema": "dittobench-coding-hosted-task-selection-v2",
            "coding_contract_version": 2,
            "shadow_only": True,
            "weight_eligible": False,
            **raw,
        }

    def digest(self) -> str:
        return coding_canonical_sha256(
            self.projection(), maximum_bytes=4096, label="hosted task selection"
        )
