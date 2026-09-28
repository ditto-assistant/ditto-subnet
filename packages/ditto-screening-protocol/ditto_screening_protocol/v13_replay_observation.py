"""Signed, exact-attempt V13 replay observations without terminal authority.

A signature authenticates who reported an observation. It does not prove that
the runner used the committed image, executed a check correctly, or satisfied
the private policy. Platform must verify those facts independently before any
future terminal use of this wire format.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

V13ReplayCheck = Literal[
    "archive_sha",
    "build_image_digest",
    "health",
    "ordinary_model_run",
    "tool_selection_run",
    "seed_memory_run",
    "two_user_isolation",
    "system_instruction_retention",
    "tool_revocation",
    "successful_duplicate_suppression",
    "same_tool_different_argument",
    "catalog_fidelity_reordering",
    "timeout_delivery_unknown",
    "fallback_evidence_retention",
    "response_field_long_answer",
    "refusal_uncertainty",
    "token_accounting",
    "opaque_inventory",
    "invariants_i1_i8_s1_s3",
    "private_metamorphic",
]
_SHA256 = r"^[0-9a-f]{64}$"
_SIGNATURE = r"^[0-9a-f]{128}$"


class V13ReplayBinding(BaseModel):
    """Expected identity sourced from Platform's current locked attempt."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    replay_id: UUID
    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: str = Field(pattern=_SHA256)
    policy_version: Literal[13] = 13
    image_sha256: str = Field(pattern=_SHA256)
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class V13ReplayObservation(BaseModel):
    """A runner claim; even ``passed`` remains unverified by this model."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    binding: V13ReplayBinding
    check_code: V13ReplayCheck
    status: Literal["passed", "failed", "inconclusive"]
    evidence_sha256: str = Field(pattern=_SHA256)
    runner_hotkey: str = Field(min_length=1, max_length=120)
    observed_at: datetime
    signature: str = Field(pattern=_SIGNATURE)

    def signing_message(self) -> bytes:
        """Domain-separated canonical bytes; excludes the signature itself."""
        content = self.model_dump(mode="json", exclude={"signature"})
        return b"ditto-v13-replay-observation:v1:\n" + json.dumps(
            content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")


def authentic_replay_observation(
    observation: V13ReplayObservation,
    *,
    expected: V13ReplayBinding,
    enrolled_runner_hotkey: str,
    source_worker_hotkey: str,
    lease_started_at: datetime,
    lease_deadline: datetime,
    verify_signature: Callable[[str, bytes, str], bool],
) -> bool:
    """Check signature, independent identity, binding, and lease time only.

    The caller must obtain every expected value from trusted Platform state.
    It must separately check actual image contents, runtime semantics, sealed
    package, broker authority, and all policy checks before terminal action.
    """
    if (
        observation.binding != expected
        or observation.runner_hotkey != enrolled_runner_hotkey
        or observation.runner_hotkey == source_worker_hotkey
        or observation.observed_at.tzinfo is None
        or lease_started_at.tzinfo is None
        or lease_deadline.tzinfo is None
        or not lease_started_at <= observation.observed_at < lease_deadline
    ):
        return False
    return verify_signature(
        observation.runner_hotkey,
        observation.signing_message(),
        observation.signature,
    )
