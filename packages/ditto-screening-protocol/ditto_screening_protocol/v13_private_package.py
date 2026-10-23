"""Immutable legacy profile and run-summary shapes for historical receipts.

The private screening audit program is retired. These models only decode
existing audit records; they cannot prepare packages or execute cases.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Sha256 = str
_SHA_PATTERN = r"^[0-9a-f]{64}$"


class V13PrivateProfile(BaseModel):
    """Published, immutable statistical design; no hidden seed or case data."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: Literal["v13-private-metamorphic-v2"] = "v13-private-metamorphic-v2"
    policy_version: Literal[13] = 13
    pairs_per_class_per_seed: Literal[10] = 10
    pairs_per_class_total: Literal[20] = 20
    independent_seed_count: Literal[2] = 2
    material_degradation_bps: Literal[1500] = 1500
    confidence_lower_bound_bps: Literal[500] = 500
    clean_control_max_degradation_bps: Literal[500] = 500
    confidence_level_bps: Literal[9500] = 9500
    # Exploratory class comparisons only. The primary is one pooled test.
    multiple_comparison_method: Literal["holm-bonferroni"] = "holm-bonferroni"
    primary_hypothesis: Literal["pooled-paired-ternary-mean"] = (
        "pooled-paired-ternary-mean"
    )
    primary_interval: Literal["one-sided-student-t"] = "one-sided-student-t"

    def checksum(self) -> Sha256:
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()


V13_PRIVATE_PROFILE = V13PrivateProfile()
V13_PRIVATE_PROFILE_SHA256 = V13_PRIVATE_PROFILE.checksum()


class V13PrivateRunSummary(BaseModel):
    """Sanitized runner output; this alone is never a V13 policy decision."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: str = Field(pattern=_SHA_PATTERN)
    image_sha256: str = Field(pattern=_SHA_PATTERN)
    profile_sha256: str = Field(pattern=_SHA_PATTERN)
    manifest_sha256: str = Field(pattern=_SHA_PATTERN)
    runner_hotkey: str = Field(min_length=1, max_length=120)
    status: Literal["completed", "failed", "inconclusive"]
    completed_pairs: int = Field(ge=0, le=512)
    evidence_sha256: str = Field(pattern=_SHA_PATTERN)
