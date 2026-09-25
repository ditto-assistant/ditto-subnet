"""Signed replay-private execution report, without terminal policy authority."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto_screening_protocol.v13_private_clean_control import (
    V13MatchedCleanControlCommitment,
)
from ditto_screening_protocol.v13_private_execute import PrivateExecutionResult
from ditto_screening_protocol.v13_private_package import V13_PRIVATE_PROFILE
from ditto_screening_protocol.v13_replay_observation import V13ReplayBinding


class V13ReplayPrivateReceipt(BaseModel):
    """Runner-signed aggregate claim bound to both exact images and sealed roles."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: Literal["v13-replay-private-receipt-v1"] = "v13-replay-private-receipt-v1"
    binding: V13ReplayBinding
    matched: V13MatchedCleanControlCommitment
    target: PrivateExecutionResult
    known_benign: PrivateExecutionResult
    runner_hotkey: str = Field(min_length=1, max_length=120)
    observed_at: datetime
    signature: str = Field(pattern=r"^[0-9a-f]{128}$")

    @model_validator(mode="after")
    def consistent_aggregate_claim(self) -> V13ReplayPrivateReceipt:
        target = self.target.summary
        clean = self.known_benign.summary
        matched = self.matched
        binding = self.binding
        if (
            matched.replay_id != binding.replay_id
            or target.agent_id != binding.agent_id
            or target.attempt_id != binding.attempt_id
            or target.artifact_sha256 != binding.artifact_sha256
            or target.image_sha256 != binding.image_sha256
            or target.agent_id != matched.target_agent_id
            or target.attempt_id != matched.target_attempt_id
            or target.artifact_sha256 != matched.target_artifact_sha256
            or target.image_sha256 != matched.target_image_sha256
            or target.manifest_sha256 != matched.target_manifest_sha256
            or clean.agent_id != matched.control_agent_id
            or clean.attempt_id != matched.control_attempt_id
            or clean.artifact_sha256 != matched.control_artifact_sha256
            or clean.image_sha256 != matched.control_image_sha256
            or clean.manifest_sha256 != matched.control_manifest_sha256
            or target.profile_sha256 != matched.profile_sha256
            or clean.profile_sha256 != matched.profile_sha256
            or target.runner_hotkey != self.runner_hotkey
            or clean.runner_hotkey != self.runner_hotkey
            or target.status != "completed"
            or clean.status != "completed"
            or target.completed_pairs != matched.pair_count
            or clean.completed_pairs != matched.pair_count
        ):
            raise ValueError("private receipt identity mismatch")
        for result in (self.target, self.known_benign):
            keys = {
                (item.transformation_class, item.seed_commitment)
                for item in result.aggregates
            }
            seeds = {item.seed_commitment for item in result.aggregates}
            if (
                len(result.aggregates) > 8
                or len(keys) != len(result.aggregates)
                or len(seeds) != V13_PRIVATE_PROFILE.independent_seed_count
                or sum(item.pairs for item in result.aggregates) != matched.pair_count
                or any(
                    item.transformation_class
                    not in {
                        "field_entity_rename",
                        "request_paraphrase",
                        "record_reorder_decoy",
                        "catalog_reorder_alias",
                    }
                    for item in result.aggregates
                )
                or any(
                    not (
                        item.pairs > 0
                        and 0
                        <= item.control_only_correct
                        <= item.control_correct
                        <= item.pairs
                        and 0
                        <= item.variant_only_correct
                        <= item.variant_correct
                        <= item.pairs
                        and item.control_correct - item.control_only_correct
                        == item.variant_correct - item.variant_only_correct
                        and item.control_correct + item.variant_only_correct
                        <= item.pairs
                    )
                    for item in result.aggregates
                )
                or any(
                    sum(
                        item.pairs
                        for item in result.aggregates
                        if item.transformation_class == class_name
                        and item.seed_commitment == seed
                    )
                    < V13_PRIVATE_PROFILE.pairs_per_class_per_seed
                    for seed in seeds
                    for class_name in (
                        "field_entity_rename",
                        "request_paraphrase",
                        "record_reorder_decoy",
                    )
                )
            ):
                raise ValueError("private receipt aggregate mismatch")
        if [
            (item.transformation_class, item.seed_commitment, item.pairs)
            for item in self.target.aggregates
        ] != [
            (item.transformation_class, item.seed_commitment, item.pairs)
            for item in self.known_benign.aggregates
        ]:
            raise ValueError("private receipt inventories differ")
        return self

    def signing_message(self) -> bytes:
        content = self.model_dump(mode="json", exclude={"signature"})
        return (
            b"ditto-v13-replay-private-receipt:v1:\n"
            + json.dumps(
                content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        )


def authentic_replay_private_receipt(
    receipt: V13ReplayPrivateReceipt,
    *,
    expected: V13ReplayBinding,
    enrolled_runner_hotkey: str,
    source_worker_hotkey: str,
    lease_started_at: datetime,
    lease_deadline: datetime,
    verify_signature: Callable[[str, bytes, str], bool],
) -> bool:
    """Authenticate the report; sealed semantics still need independent review."""
    if (
        receipt.binding != expected
        or receipt.runner_hotkey != enrolled_runner_hotkey
        or receipt.runner_hotkey == source_worker_hotkey
        or receipt.observed_at.tzinfo is None
        or lease_started_at.tzinfo is None
        or lease_deadline.tzinfo is None
        or not lease_started_at <= receipt.observed_at < lease_deadline
    ):
        return False
    return verify_signature(
        receipt.runner_hotkey, receipt.signing_message(), receipt.signature
    )
