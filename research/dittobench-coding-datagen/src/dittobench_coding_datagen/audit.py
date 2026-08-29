"""Read-only audit for the external v0.1 flat curation seed."""

from __future__ import annotations

import csv
import itertools
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.model import CorpusError

_REQUIRED_FLAT_FILES = (
    "README.md",
    "memories.csv",
    "round_001_tasks.csv",
    "security_and_balance_audit.json",
    "structural_validation_output.txt",
    "task_curation_status.csv",
    "task_manifest.csv",
    "task_selection_report.md",
    "user_profiles.csv",
)

_CLAIMED_PACKAGE_PATHS = (
    "agent_visible/memory/memory_service_seed.sqlite",
    "agent_visible/tasks/task_manifest.jsonl",
    "curator_private/rounds/round_001_hidden_labels.jsonl",
    "schemas",
    "scripts/hydrate_swebench.py",
    "scripts/validate_dataset.py",
    "provenance",
)


def _rows(root: Path, name: str) -> list[dict[str, str]]:
    try:
        with (root / name).open(newline="", encoding="utf-8") as source:
            return list(csv.DictReader(source))
    except OSError as error:
        raise CorpusError(f"could not read {name}: {error}") from error


def _finding(severity: str, code: str, message: str, **evidence: Any) -> dict[str, Any]:
    return {
        "code": code,
        "evidence": evidence,
        "message": message,
        "severity": severity,
    }


def audit_curation_seed(root: Path) -> dict[str, Any]:
    root = root.resolve()
    missing_flat = [
        name for name in _REQUIRED_FLAT_FILES if not (root / name).is_file()
    ]
    if missing_flat:
        raise CorpusError(
            f"curation seed is missing required flat files: {missing_flat}"
        )

    tasks = _rows(root, "task_manifest.csv")
    round_tasks = _rows(root, "round_001_tasks.csv")
    profiles = _rows(root, "user_profiles.csv")
    memories = _rows(root, "memories.csv")
    status = _rows(root, "task_curation_status.csv")
    policy = {row["task_id"]: row["policy"] for row in status}
    findings: list[dict[str, Any]] = []

    missing_claimed = [
        name for name in _CLAIMED_PACKAGE_PATHS if not (root / name).exists()
    ]
    if missing_claimed:
        findings.append(
            _finding(
                "blocker",
                "PACKAGE-INCOMPLETE",
                "local files do not contain the package tree described by README",
                missing=missing_claimed,
            )
        )

    public_ids = [
        row.get("instance_id", "")
        for row in tasks
        if "__" in row.get("instance_id", "")
    ]
    if public_ids:
        findings.append(
            _finding(
                "blocker",
                "PUBLIC-UPSTREAM-IDENTITIES",
                "task manifest exposes lookup-answerable upstream instance ids",
                count=len(public_ids),
            )
        )
    mutable_images = [
        row.get("docker_image", "")
        for row in tasks
        if row.get("docker_image", "").endswith(":latest")
    ]
    if mutable_images:
        findings.append(
            _finding(
                "blocker",
                "MUTABLE-TASK-IMAGES",
                "task environments use mutable image tags instead of OCI digests",
                count=len(mutable_images),
            )
        )

    not_ready = [
        row["task_id"]
        for row in tasks
        if row.get("leaderboard_ready", "").casefold() != "true"
    ]
    if not_ready:
        findings.append(
            _finding(
                "blocker",
                "TASKS-NOT-READY",
                "tasks are not marked leaderboard ready",
                count=len(not_ready),
            )
        )

    template_policies: dict[str, set[str]] = defaultdict(set)
    for row in round_tasks:
        template_policies[row.get("task_instruction", "")].add(
            policy.get(row["task_id"], "missing")
        )
    pure_templates = sum(1 for values in template_policies.values() if len(values) == 1)
    if template_policies and pure_templates == len(template_policies):
        findings.append(
            _finding(
                "blocker",
                "POLICY-TEMPLATE-LEAK",
                "every public instruction template identifies exactly one "
                "private policy",
                task_count=len(round_tasks),
                template_count=len(template_policies),
            )
        )

    content_counts = Counter(row.get("content", "") for row in memories)
    repository_memories = [row for row in memories if row.get("scope") == "repository"]
    repository_unique = len({row.get("content", "") for row in repository_memories})
    missing_validity = sum(
        not row.get("valid_from_commit") or not row.get("valid_until_commit")
        for row in repository_memories
    )
    missing_supersession = sum(
        row.get("supersedes", "") in {"", "[]"} for row in repository_memories
    )
    if repository_memories and (missing_validity or missing_supersession):
        findings.append(
            _finding(
                "blocker",
                "MEMORY-PROVENANCE-MISSING",
                "repository memories lack validity and supersession evidence",
                records=len(repository_memories),
                unique_content=repository_unique,
                missing_validity=missing_validity,
                missing_supersession=missing_supersession,
            )
        )

    stale_fingerprints = sum(
        "stale" in row.get("slot", "").casefold()
        and row.get("type") == "failed_approach"
        and row.get("confidence") == "0.42"
        for row in memories
    )
    if stale_fingerprints:
        findings.append(
            _finding(
                "high",
                "STALE-LABEL-FINGERPRINT",
                "stale memories expose their classification through redundant metadata",
                count=stale_fingerprints,
            )
        )

    known: dict[str, set[str]] = {}
    for row in profiles:
        try:
            known[row["user_id"]] = set(json.loads(row.get("known_repositories", "[]")))
        except json.JSONDecodeError as error:
            raise CorpusError(
                f"invalid known_repositories for {row.get('user_id')}"
            ) from error
    triple_counts = Counter(
        len(set().union(*(known[user] for user in group)))
        for group in itertools.combinations(sorted(known), 3)
    )
    if triple_counts and min(triple_counts) > 3:
        findings.append(
            _finding(
                "high",
                "PRACTICE-USERS-REVEAL-REPOSITORIES",
                "every three-profile subset names substantially more than "
                "three repositories",
                repository_union_distribution=dict(sorted(triple_counts.items())),
            )
        )

    active_users = {row.get("active_user_id") for row in round_tasks}
    if len(active_users) != len(profiles):
        findings.append(
            _finding(
                "medium",
                "ROUND-USER-COVERAGE",
                "round does not exercise every declared user profile",
                active_user_count=len(active_users),
                profile_count=len(profiles),
            )
        )

    return {
        "counts": {
            "memory_records": len(memories),
            "memory_unique_content": len(content_counts),
            "profiles": len(profiles),
            "round_tasks": len(round_tasks),
            "tasks": len(tasks),
        },
        "findings": findings,
        "input_root": str(root),
        "status": "BLOCKED"
        if any(item["severity"] == "blocker" for item in findings)
        else "PASS",
    }
