"""Strict practice source and emitted-pack validation."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    safe_relative_path,
    tree_identities,
)
from dittobench_coding_datagen.fixtures import fixture_kinds
from dittobench_coding_datagen.model import (
    CODING_CONTRACT_VERSION,
    MEMORY_CONDITIONS,
    PRACTICE_AGENT_INSTRUCTION,
    PRACTICE_SCHEMA,
    CorpusError,
    PracticeSource,
)

_USER_ID = re.compile(r"^P0[1-3]$")
_REPOSITORY_ID = re.compile(r"^practice-[a-z][a-z0-9-]{1,48}$")
_TASK_ID = re.compile(r"^PRACTICE-[A-Z0-9-]{3,64}$")
_MEMORY_ID = re.compile(r"^P0[1-3]-M[0-9]{3}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PACK_ID = re.compile(r"^coding-practice-3x3-v[1-9][0-9]*$")

_SOURCE_KEYS = frozenset(
    {
        "coding_contract_version",
        "memories",
        "practice_pack_id",
        "schema",
        "tasks",
        "users",
        "weight_eligible",
    }
)
_USER_KEYS = frozenset({"display_name", "known_repositories", "summary", "user_id"})
_MEMORY_KEYS = frozenset(
    {
        "content",
        "memory_id",
        "owner_user_id",
        "repository_id",
        "supersedes",
        "valid_from_revision",
        "valid_until_revision",
    }
)
_SOURCE_TASK_KEYS = frozenset(
    {
        "active_user_id",
        "base_revision",
        "fixture",
        "memory_condition",
        "problem_statement",
        "repository_id",
        "task_id",
    }
)
_AGENT_TASK_KEYS = frozenset(
    {
        "active_user_id",
        "base_revision",
        "instruction",
        "problem_statement",
        "repository_id",
        "task_id",
        "visible_capsule",
    }
)
_GRADER_TASK_KEYS = frozenset(
    {
        "base_revision",
        "fixture",
        "grader_files",
        "memory_condition",
        "repository_id",
        "task_id",
    }
)
_MANIFEST_KEYS = frozenset(
    {
        "coding_contract_version",
        "corpus_scope",
        "files",
        "memory_count",
        "practice_pack_id",
        "schema",
        "source_sha256",
        "task_count",
        "user_count",
        "weight_eligible",
    }
)
_FILE_IDENTITY_KEYS = frozenset({"path", "sha256", "size_bytes"})

_FORBIDDEN_AGENT_KEYS = frozenset(
    {
        "base_commit",
        "fail_to_pass",
        "gold_patch",
        "grader_tests",
        "hidden_tests",
        "instance_id",
        "issue_url",
        "memory_condition",
        "pass_to_pass",
        "policy",
        "reference_patch",
        "source_ref",
        "test_patch",
    }
)

_FORBIDDEN_AGENT_FRAGMENTS = (
    "github.com/",
    "gold patch",
    "swe-bench",
    "swebench",
    "must_use",
    "helpful_use",
    "selective_use",
    "optional_use",
    "must_abstain",
    "stale_memory_trap",
    "astropy/astropy",
    "django/django",
    "matplotlib/matplotlib",
    "pydata/xarray",
    "pytest-dev/pytest",
    "pylint-dev/pylint",
    "scikit-learn/scikit-learn",
    "sphinx-doc/sphinx",
    "sympy/sympy",
    "psf/requests",
)


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CorpusError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise CorpusError(f"{field} must be an array")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorpusError(f"{field} must be a non-empty string")
    return value


def _unique(records: Iterable[dict[str, Any]], key: str, field: str) -> None:
    values = [_string(record.get(key), f"{field}.{key}") for record in records]
    if len(values) != len(set(values)):
        raise CorpusError(f"{field}.{key} values must be unique")


def _exact_keys(record: dict[str, Any], expected: frozenset[str], field: str) -> None:
    observed = frozenset(record)
    if observed != expected:
        missing = sorted(expected - observed)
        unknown = sorted(observed - expected)
        raise CorpusError(
            f"{field} fields do not match the contract; "
            f"missing={missing}, unknown={unknown}"
        )


def load_practice_source(path: Path) -> PracticeSource:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusError(f"could not read practice source {path}: {error}") from error
    root = _object(raw, "source")
    _exact_keys(root, _SOURCE_KEYS, "source")
    if root.get("schema") != PRACTICE_SCHEMA:
        raise CorpusError(f"source.schema must be {PRACTICE_SCHEMA!r}")
    if root.get("coding_contract_version") != CODING_CONTRACT_VERSION:
        raise CorpusError(
            f"source.coding_contract_version must be {CODING_CONTRACT_VERSION}"
        )
    if root.get("weight_eligible") is not False:
        raise CorpusError("public practice source must set weight_eligible=false")
    users = tuple(
        _object(value, "users[]") for value in _list(root.get("users"), "users")
    )
    memories = tuple(
        _object(value, "memories[]")
        for value in _list(root.get("memories"), "memories")
    )
    tasks = tuple(
        _object(value, "tasks[]") for value in _list(root.get("tasks"), "tasks")
    )
    source = PracticeSource(
        pack_id=_string(root.get("practice_pack_id"), "practice_pack_id"),
        users=users,
        memories=memories,
        tasks=tasks,
        source_path=path,
    )
    if not _PACK_ID.fullmatch(source.pack_id):
        raise CorpusError("practice_pack_id must identify the versioned 3x3 pack")
    validate_practice_source(source)
    return source


def validate_practice_source(source: PracticeSource) -> None:
    if len(source.users) != 3:
        raise CorpusError("public practice must contain exactly three users")
    _unique(source.users, "user_id", "users")
    user_ids = {_string(user.get("user_id"), "users.user_id") for user in source.users}
    if any(not _USER_ID.fullmatch(user_id) for user_id in user_ids):
        raise CorpusError("public practice user ids must be P01, P02, and P03")

    repositories: set[str] = set()
    user_repositories: dict[str, set[str]] = {}
    for user in source.users:
        _exact_keys(user, _USER_KEYS, "users[]")
        user_id = _string(user.get("user_id"), "users.user_id")
        _string(user.get("display_name"), "users.display_name")
        _string(user.get("summary"), "users.summary")
        known = _list(user.get("known_repositories"), "users.known_repositories")
        user_known: set[str] = set()
        for repository in known:
            repository_id = _string(repository, "users.known_repositories[]")
            if not _REPOSITORY_ID.fullmatch(repository_id):
                raise CorpusError(f"invalid practice repository id: {repository_id!r}")
            repositories.add(repository_id)
            user_known.add(repository_id)
        if len(known) != 2 or len(user_known) != 2:
            raise CorpusError("every practice user must know exactly two repositories")
        user_repositories[user_id] = user_known
    if len(repositories) != 3:
        raise CorpusError(
            "public practice users must reference exactly three repositories"
        )

    if len(source.memories) != 18:
        raise CorpusError("public practice must contain exactly eighteen memories")
    _unique(source.memories, "memory_id", "memories")
    memory_texts: set[str] = set()
    memories_by_id = {
        _string(memory.get("memory_id"), "memories.memory_id"): memory
        for memory in source.memories
    }
    for memory in source.memories:
        _exact_keys(memory, _MEMORY_KEYS, "memories[]")
        memory_id = _string(memory.get("memory_id"), "memories.memory_id")
        if not _MEMORY_ID.fullmatch(memory_id):
            raise CorpusError(f"invalid practice memory id: {memory_id!r}")
        owner = _string(memory.get("owner_user_id"), "memories.owner_user_id")
        if owner not in user_ids:
            raise CorpusError(f"memory {memory_id} names unknown owner {owner}")
        content = _string(memory.get("content"), "memories.content")
        if content in memory_texts:
            raise CorpusError("public practice memory content must be unique")
        memory_texts.add(content)
        repository = memory.get("repository_id")
        if repository is not None and repository not in repositories:
            raise CorpusError(
                f"memory {memory_id} names unknown repository {repository!r}"
            )
        if repository is not None and repository not in user_repositories[owner]:
            raise CorpusError(
                f"memory {memory_id} is outside owner {owner}'s repositories"
            )
        valid_from = _string(
            memory.get("valid_from_revision"), "memories.valid_from_revision"
        )
        if repository is not None and not valid_from.startswith(str(repository)):
            raise CorpusError(
                f"memory {memory_id} validity is not scoped to {repository!r}"
            )
        valid_until = memory.get("valid_until_revision")
        if valid_until is not None:
            valid_until = _string(valid_until, "memories.valid_until_revision")
            if repository is not None and not valid_until.startswith(str(repository)):
                raise CorpusError(
                    f"memory {memory_id} validity end is not scoped to {repository!r}"
                )
        supersedes = _list(memory.get("supersedes", []), "memories.supersedes")
        if any(not isinstance(value, str) for value in supersedes):
            raise CorpusError(f"memory {memory_id} supersedes must contain ids")
        for previous_id in supersedes:
            previous = memories_by_id.get(previous_id)
            if previous is None:
                raise CorpusError(
                    f"memory {memory_id} supersedes unknown {previous_id}"
                )
            if previous.get("owner_user_id") != owner:
                raise CorpusError(f"memory {memory_id} crosses owners in supersedes")
            if previous.get("repository_id") != repository:
                raise CorpusError(
                    f"memory {memory_id} crosses repositories in supersedes"
                )
            if not previous.get("valid_until_revision"):
                raise CorpusError(
                    f"superseded memory {previous_id} has no validity end"
                )
    if set(Counter(memory["owner_user_id"] for memory in source.memories).values()) != {
        6
    }:
        raise CorpusError("every practice user must own exactly six memories")

    if len(source.tasks) != 9:
        raise CorpusError("public practice must contain exactly nine tasks")
    _unique(source.tasks, "task_id", "tasks")
    task_repositories: set[str] = set()
    conditions: set[str] = set()
    task_users: set[str] = set()
    fixtures: set[str] = set()
    for task in source.tasks:
        _exact_keys(task, _SOURCE_TASK_KEYS, "tasks[]")
        task_id = _string(task.get("task_id"), "tasks.task_id")
        if not _TASK_ID.fullmatch(task_id):
            raise CorpusError(f"invalid opaque practice task id: {task_id!r}")
        repository = _string(task.get("repository_id"), "tasks.repository_id")
        if repository not in repositories:
            raise CorpusError(f"task {task_id} names unknown repository {repository}")
        task_repositories.add(repository)
        user_id = _string(task.get("active_user_id"), "tasks.active_user_id")
        if user_id not in user_ids:
            raise CorpusError(f"task {task_id} names unknown active user {user_id}")
        task_users.add(user_id)
        condition = _string(task.get("memory_condition"), "tasks.memory_condition")
        if condition not in MEMORY_CONDITIONS:
            raise CorpusError(
                f"task {task_id} has invalid memory condition {condition!r}"
            )
        conditions.add(condition)
        knows_repository = repository in user_repositories[user_id]
        if condition == "irrelevant" and knows_repository:
            raise CorpusError(
                f"irrelevant task {task_id} is assigned to a "
                "repository-experienced user"
            )
        if condition != "irrelevant" and not knows_repository:
            raise CorpusError(
                f"memory-relevant task {task_id} is assigned to an inexperienced user"
            )
        fixture = _string(task.get("fixture"), "tasks.fixture")
        if fixture not in fixture_kinds():
            raise CorpusError(f"task {task_id} has unknown fixture {fixture!r}")
        if fixture in fixtures:
            raise CorpusError(f"fixture {fixture!r} is assigned more than once")
        fixtures.add(fixture)
        _string(task.get("problem_statement"), "tasks.problem_statement")
        base_revision = _string(task.get("base_revision"), "tasks.base_revision")
        if not base_revision.startswith(repository):
            raise CorpusError(
                f"task {task_id} base revision is outside repository {repository!r}"
            )
    if task_repositories != repositories:
        raise CorpusError("every practice repository must have a task")
    if task_users != user_ids:
        raise CorpusError("every practice user must own a task")
    if set(Counter(task["active_user_id"] for task in source.tasks).values()) != {3}:
        raise CorpusError("every practice user must own exactly three tasks")
    if set(Counter(task["repository_id"] for task in source.tasks).values()) != {3}:
        raise CorpusError("every practice repository must own exactly three tasks")
    if conditions != MEMORY_CONDITIONS:
        raise CorpusError("public practice must cover every memory condition")


def _walk_json(value: Any, *, path: str = "$") -> Iterable[tuple[str, str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield child_path, str(key), child
            yield from _walk_json(child, path=child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_json(child, path=f"{path}[{index}]")


def assert_agent_view_safe(value: Any, *, source: str) -> None:
    for path, key, child in _walk_json(value):
        if key.casefold() in _FORBIDDEN_AGENT_KEYS:
            raise CorpusError(f"{source}: forbidden miner-facing key at {path}")
        if not isinstance(child, str):
            continue
        folded = child.casefold()
        for fragment in _FORBIDDEN_AGENT_FRAGMENTS:
            if fragment in folded:
                raise CorpusError(
                    f"{source}: forbidden miner-facing fragment {fragment!r} at {path}"
                )


def assert_agent_text_safe(value: str, *, source: str) -> None:
    folded = value.casefold()
    for fragment in _FORBIDDEN_AGENT_FRAGMENTS:
        if fragment in folded:
            raise CorpusError(f"{source}: forbidden miner-facing fragment {fragment!r}")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line:
                continue
            value = json.loads(line)
            record = _object(value, f"{path}:{number}")
            if canonical_json_bytes(record).rstrip(b"\n") != line.encode("utf-8"):
                raise CorpusError(f"{path}:{number} is not canonical JSON")
            records.append(record)
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusError(f"could not read JSONL {path}: {error}") from error
    return records


def validate_pack(root: Path) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    try:
        body = manifest_path.read_bytes()
        manifest = _object(json.loads(body), "manifest")
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusError(f"could not read pack manifest: {error}") from error
    if canonical_json_bytes(manifest) != body:
        raise CorpusError("manifest.json is not canonical")
    _exact_keys(manifest, _MANIFEST_KEYS, "manifest")
    if manifest.get("schema") != PRACTICE_SCHEMA:
        raise CorpusError("pack schema is not the public practice schema")
    if manifest.get("coding_contract_version") != CODING_CONTRACT_VERSION:
        raise CorpusError("pack coding contract version is unsupported")
    if manifest.get("corpus_scope") != "public_practice":
        raise CorpusError("public pack corpus_scope must be public_practice")
    if manifest.get("weight_eligible") is not False:
        raise CorpusError("public practice pack must never be weight eligible")
    pack_id = _string(manifest.get("practice_pack_id"), "manifest.practice_pack_id")
    if not _PACK_ID.fullmatch(pack_id):
        raise CorpusError(
            "manifest practice_pack_id must identify the versioned 3x3 pack"
        )
    validate_sha256(
        _string(manifest.get("source_sha256"), "manifest.source_sha256"),
        "manifest.source_sha256",
    )

    expected_files = manifest.get("files")
    if not isinstance(expected_files, list):
        raise CorpusError("manifest.files must be an array")
    for index, value in enumerate(expected_files):
        identity = _object(value, f"manifest.files[{index}]")
        _exact_keys(identity, _FILE_IDENTITY_KEYS, f"manifest.files[{index}]")
        safe_relative_path(_string(identity.get("path"), "manifest.files[].path"))
        validate_sha256(
            _string(identity.get("sha256"), "manifest.files[].sha256"),
            "manifest.files[].sha256",
        )
        size = identity.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise CorpusError("manifest.files[].size_bytes must be non-negative")
    observed_identities = tree_identities(root, exclude=frozenset({"manifest.json"}))
    observed_files = [identity.as_json() for identity in observed_identities]
    if expected_files != observed_files:
        raise CorpusError("pack file identities do not match manifest")

    users = _load_jsonl(root / "agent/users.jsonl")
    memories = _load_jsonl(root / "agent/memories.jsonl")
    tasks = _load_jsonl(root / "agent/tasks.jsonl")
    grader = _load_jsonl(root / "grader/tasks.jsonl")
    for user in users:
        _exact_keys(user, _USER_KEYS, "agent/users.jsonl[]")
    for memory in memories:
        _exact_keys(memory, _MEMORY_KEYS, "agent/memories.jsonl[]")
    for task in tasks:
        _exact_keys(task, _AGENT_TASK_KEYS, "agent/tasks.jsonl[]")
    for task in grader:
        _exact_keys(task, _GRADER_TASK_KEYS, "grader/tasks.jsonl[]")
    for name, records in (
        ("agent/users.jsonl", users),
        ("agent/memories.jsonl", memories),
        ("agent/tasks.jsonl", tasks),
    ):
        assert_agent_view_safe(records, source=name)
    if len(users) != 3 or len({record.get("user_id") for record in users}) != 3:
        raise CorpusError("compiled pack must contain three unique users")
    repositories = {
        repository
        for user in users
        for repository in _list(user.get("known_repositories"), "known_repositories")
    }
    if len(repositories) != 3:
        raise CorpusError("compiled pack must expose only three practice repositories")
    if len(tasks) != 9 or len(tasks) != len(grader):
        raise CorpusError("compiled task and grader indexes are incomplete")
    task_ids = {record.get("task_id") for record in tasks}
    if task_ids != {record.get("task_id") for record in grader}:
        raise CorpusError("agent and grader task indexes disagree")
    grader_by_id = {record.get("task_id"): record for record in grader}
    if len(grader_by_id) != len(grader):
        raise CorpusError("grader task ids must be unique")
    reconstructed_tasks: list[dict[str, Any]] = []
    allowed_capsule_prefixes: set[str] = set()
    for task_id in task_ids:
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise CorpusError(f"compiled pack contains invalid task id {task_id!r}")
        task = next(record for record in tasks if record.get("task_id") == task_id)
        expected_capsule = f"capsules/{task_id}/visible"
        if task.get("visible_capsule") != expected_capsule:
            raise CorpusError(
                f"task {task_id} visible_capsule must be {expected_capsule!r}"
            )
        if task.get("instruction") != PRACTICE_AGENT_INSTRUCTION:
            raise CorpusError(f"task {task_id} uses a non-canonical instruction")
        grader_task = grader_by_id[task_id]
        reconstructed_tasks.append(
            {
                "active_user_id": task.get("active_user_id"),
                "base_revision": task.get("base_revision"),
                "fixture": grader_task.get("fixture"),
                "memory_condition": grader_task.get("memory_condition"),
                "problem_statement": task.get("problem_statement"),
                "repository_id": task.get("repository_id"),
                "task_id": task_id,
            }
        )
        visible = root / "capsules" / task_id / "visible"
        hidden = root / "capsules" / task_id / "grader"
        if not visible.is_dir() or not hidden.is_dir():
            raise CorpusError(f"task {task_id} capsule is incomplete")
        allowed_capsule_prefixes.update(
            {
                f"capsules/{task_id}/visible/workspace/",
                f"capsules/{task_id}/grader/",
            }
        )
        expected_grader_files = [
            identity.as_json() for identity in tree_identities(hidden)
        ]
        if grader_task.get("grader_files") != expected_grader_files:
            raise CorpusError(f"task {task_id} grader file identities disagree")
    fixed_files = {
        "agent/memories.jsonl",
        "agent/tasks.jsonl",
        "agent/users.jsonl",
        "grader/tasks.jsonl",
    }
    for observed_identity in observed_identities:
        if observed_identity.path in fixed_files:
            continue
        if not any(
            observed_identity.path.startswith(prefix)
            for prefix in allowed_capsule_prefixes
        ):
            raise CorpusError(
                f"pack contains an unexpected file: {observed_identity.path}"
            )
        if "/visible/workspace/" in observed_identity.path:
            try:
                text = (root / observed_identity.path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise CorpusError(
                    "miner-visible practice file is not UTF-8: "
                    f"{observed_identity.path}"
                ) from error
            assert_agent_text_safe(text, source=observed_identity.path)
    validate_practice_source(
        PracticeSource(
            pack_id=pack_id,
            users=tuple(users),
            memories=tuple(memories),
            tasks=tuple(reconstructed_tasks),
            source_path=manifest_path,
        )
    )
    if manifest.get("user_count") != len(users):
        raise CorpusError("manifest user_count is incorrect")
    if manifest.get("memory_count") != len(memories):
        raise CorpusError("manifest memory_count is incorrect")
    if manifest.get("task_count") != len(tasks):
        raise CorpusError("manifest task_count is incorrect")
    return manifest


def validate_sha256(value: str, field: str) -> None:
    if not _SHA256.fullmatch(value):
        raise CorpusError(f"{field} must be a lowercase SHA-256")


def validate_capsule_path(value: str, field: str) -> str:
    try:
        return safe_relative_path(value)
    except CorpusError as error:
        raise CorpusError(f"{field}: {error}") from error
