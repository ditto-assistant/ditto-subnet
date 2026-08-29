"""Safe public-practice case views for agent and grader runtimes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    sha256_hex,
    tree_identities,
)
from dittobench_coding_datagen.model import CODING_CONTRACT_VERSION, CorpusError
from dittobench_coding_datagen.validation import validate_pack


@dataclass(frozen=True)
class PracticeRuntimePolicy:
    """Manifest-bound public permissions consumed by the practice runner."""

    editable_paths: tuple[str, ...]
    test_command_ids: tuple[str, ...]
    build_command_ids: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "build_command_ids": list(self.build_command_ids),
            "editable_paths": list(self.editable_paths),
            "test_command_ids": list(self.test_command_ids),
        }


@dataclass(frozen=True)
class PracticeAgentCase:
    """Miner-visible case view with only the active user's memory bundle."""

    pack_id: str
    task_id: str
    active_user_id: str
    base_revision: str
    repository_id: str
    instruction: str
    problem_statement: str
    visible_capsule: str
    runtime_policy: PracticeRuntimePolicy
    memories: tuple[dict[str, Any], ...]
    memory_bundle_sha256: str
    visible_bundle_sha256: str

    @property
    def ticket_id(self) -> str:
        return f"practice:{self.pack_id}:{self.task_id}"

    @property
    def protocol_memories(self) -> tuple[dict[str, Any], ...]:
        """Project pack records onto the contract's miner-visible schema."""

        return tuple(_protocol_memory(record) for record in self.memories)

    def seed_request(self) -> dict[str, Any]:
        return {
            "case_id": self.task_id,
            "coding_contract_version": CODING_CONTRACT_VERSION,
            "memories": list(self.protocol_memories),
            "memory_bundle_sha256": self.memory_bundle_sha256,
            "profile_capability_id": self.active_user_id,
            "ticket_id": self.ticket_id,
        }

    def run_request(
        self, *, workspace_capability_url: str, inference_base_url: str
    ) -> dict[str, Any]:
        return {
            "budgets": {
                "model_input_tokens": 32_000,
                "model_output_tokens": 4_000,
                "wall_time_seconds": 120,
                "workspace_tool_calls": 64,
            },
            "case_id": self.task_id,
            "coding_contract_version": CODING_CONTRACT_VERSION,
            "inference_base_url": inference_base_url,
            "issue": {
                "constraints": [self.instruction],
                "description": self.problem_statement,
                "title": self.task_id,
            },
            "profile_capability_id": self.active_user_id,
            "repository_epoch": self.base_revision,
            "runtime_policy": self.runtime_policy.as_json(),
            "ticket_id": self.ticket_id,
            "visible_bundle_sha256": self.visible_bundle_sha256,
            "workspace_capability_url": workspace_capability_url,
        }


@dataclass(frozen=True)
class PracticeGraderCase:
    """Grader-only case view; never serialize this object to a harness."""

    task_id: str
    fixture: str
    grader_root: Path
    grader_files: tuple[dict[str, Any], ...]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusError(f"could not read practice index {path}: {error}") from error


def _one(records: list[dict[str, Any]], key: str, value: str) -> dict[str, Any]:
    matches = [record for record in records if record.get(key) == value]
    if len(matches) != 1:
        raise CorpusError(f"expected one {key}={value!r}, found {len(matches)}")
    return matches[0]


def _protocol_memory(record: dict[str, Any]) -> dict[str, Any]:
    repository_id = record.get("repository_id")
    return {
        "confidence_micros": 900_000,
        "content": record["content"],
        "fact_group_id": None,
        "memory_id": record["memory_id"],
        "repository_capability_id": repository_id,
        "scope": "repository" if repository_id is not None else "profile",
        "supersedes": record.get("supersedes", []),
        "type": "project_experience" if repository_id is not None else "user_workflow",
        "valid_from_epoch": record.get("valid_from_revision"),
        "valid_until_epoch": record.get("valid_until_revision"),
    }


def load_practice_agent_case(pack: Path, task_id: str) -> PracticeAgentCase:
    """Build one agent view without incorporating grader metadata."""

    manifest = validate_pack(pack)
    task = _one(_load_jsonl(pack / "agent/tasks.jsonl"), "task_id", task_id)
    active_user_id = str(task["active_user_id"])
    memories = tuple(
        record
        for record in _load_jsonl(pack / "agent/memories.jsonl")
        if record.get("owner_user_id") == active_user_id
    )
    if len(memories) != 6:
        raise CorpusError(
            f"practice case {task_id} must expose exactly six active-user memories"
        )
    runtime = task["runtime_policy"]
    runtime_policy = PracticeRuntimePolicy(
        editable_paths=tuple(runtime["editable_paths"]),
        test_command_ids=tuple(runtime["test_command_ids"]),
        build_command_ids=tuple(runtime["build_command_ids"]),
    )
    protocol_memories = [_protocol_memory(record) for record in memories]
    memory_bundle_sha256 = sha256_hex(
        canonical_json_bytes({"memories": protocol_memories})
    )
    visible_root = pack / str(task["visible_capsule"])
    visible_identity = {
        "files": [identity.as_json() for identity in tree_identities(visible_root)],
        "task": task,
    }
    return PracticeAgentCase(
        pack_id=str(manifest["practice_pack_id"]),
        task_id=task_id,
        active_user_id=active_user_id,
        base_revision=str(task["base_revision"]),
        repository_id=str(task["repository_id"]),
        instruction=str(task["instruction"]),
        problem_statement=str(task["problem_statement"]),
        visible_capsule=str(task["visible_capsule"]),
        runtime_policy=runtime_policy,
        memories=memories,
        memory_bundle_sha256=memory_bundle_sha256,
        visible_bundle_sha256=sha256_hex(canonical_json_bytes(visible_identity)),
    )


def load_practice_grader_case(pack: Path, task_id: str) -> PracticeGraderCase:
    """Load grader metadata only inside the trusted practice grader."""

    validate_pack(pack)
    record = _one(_load_jsonl(pack / "grader/tasks.jsonl"), "task_id", task_id)
    return PracticeGraderCase(
        task_id=task_id,
        fixture=str(record["fixture"]),
        grader_root=pack / "capsules" / task_id / "grader",
        grader_files=tuple(record["grader_files"]),
    )
