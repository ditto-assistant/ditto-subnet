from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    safe_relative_path,
    tree_identities,
)
from dittobench_coding_datagen.compiler import compile_practice, grade, materialize
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.validation import (
    assert_agent_view_safe,
    load_practice_source,
    validate_pack,
    validate_practice_source,
)

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "practice-source/source.json"


def test_published_schemas_accept_the_committed_source_and_manifest() -> None:
    for schema_name, document in (
        ("practice-source.schema.json", SOURCE),
        ("practice-manifest.schema.json", ROOT / "practice/v1/manifest.json"),
    ):
        schema = json.loads((ROOT / "schemas" / schema_name).read_text())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(json.loads(document.read_text()))


def test_practice_source_is_disjoint_and_complete() -> None:
    source = load_practice_source(SOURCE)
    repositories = {
        repository for user in source.users for repository in user["known_repositories"]
    }
    assert repositories == {"practice-ledger", "practice-config", "practice-cache"}
    assert {task["memory_condition"] for task in source.tasks} == {
        "required_constraint",
        "relevant_nonrequired",
        "irrelevant",
        "stale_conflicting",
        "current_override",
    }
    assert len(source.tasks) == 9
    assert len(source.memories) == len(
        {memory["content"] for memory in source.memories}
    )


def test_compile_is_deterministic_and_agent_view_is_blind(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = compile_practice(SOURCE, first)
    second_manifest = compile_practice(SOURCE, second)

    assert first_manifest == second_manifest
    assert (first / "manifest.json").read_bytes() == canonical_json_bytes(
        first_manifest
    )
    assert validate_pack(first) == first_manifest
    assert validate_pack(second) == second_manifest

    agent_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((first / "agent").glob("*.jsonl"))
    ).casefold()
    for forbidden in (
        "memory_condition",
        "grader_tests",
        "gold_patch",
        "instance_id",
        "issue_url",
        "swebench",
        "must_use",
    ):
        assert forbidden not in agent_text


def test_committed_practice_pack_matches_the_compiler(tmp_path: Path) -> None:
    rebuilt = tmp_path / "rebuilt"
    compile_practice(SOURCE, rebuilt)
    committed = ROOT / "practice/v1"

    assert validate_pack(committed) == validate_pack(rebuilt)
    assert [identity.as_json() for identity in tree_identities(committed)] == [
        identity.as_json() for identity in tree_identities(rebuilt)
    ]


def test_pack_tampering_is_detected(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    compile_practice(SOURCE, pack)
    task_index = pack / "agent/tasks.jsonl"
    task_index.write_text(
        task_index.read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )
    with pytest.raises(CorpusError, match="file identities"):
        validate_pack(pack)


def test_pack_rejects_capsule_path_traversal_even_with_matching_hashes(
    tmp_path: Path,
) -> None:
    pack = tmp_path / "pack"
    compile_practice(SOURCE, pack)
    task_index = pack / "agent/tasks.jsonl"
    records = [json.loads(line) for line in task_index.read_text().splitlines()]
    records[0]["visible_capsule"] = "../../outside"
    task_index.write_bytes(b"".join(canonical_json_bytes(record) for record in records))

    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"] = [
        identity.as_json()
        for identity in tree_identities(pack, exclude=frozenset({"manifest.json"}))
    ]
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(CorpusError, match="visible_capsule"):
        validate_pack(pack)


def test_pack_rejects_unexpected_miner_visible_file_even_when_manifested(
    tmp_path: Path,
) -> None:
    pack = tmp_path / "pack"
    compile_practice(SOURCE, pack)
    leaked = pack / "capsules/PRACTICE-LEDGER-001/visible/workspace/answer.txt"
    leaked.write_text("gold patch", encoding="utf-8")

    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"] = [
        identity.as_json()
        for identity in tree_identities(pack, exclude=frozenset({"manifest.json"}))
    ]
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(CorpusError, match="forbidden miner-facing fragment"):
        validate_pack(pack)


def test_source_rejects_unbalanced_repository_tasks() -> None:
    source = load_practice_source(SOURCE)
    tasks = [dict(task) for task in source.tasks]
    tasks[0]["repository_id"] = "practice-config"
    tasks[0]["base_revision"] = "practice-config-v2"

    with pytest.raises(CorpusError, match="fixture|repository"):
        validate_practice_source(replace(source, tasks=tuple(tasks)))


@pytest.mark.parametrize(
    ("task_id", "fixed_source"),
    [
        (
            "PRACTICE-LEDGER-001",
            "def normalize_reference(value: str) -> str:\n    return value.strip()\n",
        ),
        (
            "PRACTICE-LEDGER-002",
            "def allocate_cents(total: int, parties: int) -> list[int]:\n"
            "    share, remainder = divmod(total, parties)\n"
            "    return [share + (index < remainder) for index in range(parties)]\n",
        ),
        (
            "PRACTICE-LEDGER-003",
            "def is_balanced(debits: list[int], credits: list[int]) -> bool:\n"
            "    return sum(debits) == sum(credits)\n",
        ),
        (
            "PRACTICE-CONFIG-001",
            "def merge_config(defaults: dict, environment: dict) -> dict:\n"
            "    result = dict(defaults)\n"
            "    result.update(environment)\n"
            "    return result\n",
        ),
        (
            "PRACTICE-CONFIG-002",
            "def parse_bool(value: str) -> bool:\n"
            "    normalized = value.strip().lower()\n"
            "    if normalized not in {'true', 'false'}:\n"
            "        raise ValueError('unsupported boolean')\n"
            "    return normalized == 'true'\n",
        ),
        (
            "PRACTICE-CONFIG-003",
            "def canonical_endpoint(value: str) -> str:\n"
            "    return value.rstrip('/')\n",
        ),
        (
            "PRACTICE-CACHE-001",
            "def cache_key(namespace: str, item: str) -> str:\n"
            "    return f'{namespace.strip().lower()}:{item.strip()}'\n",
        ),
        (
            "PRACTICE-CACHE-002",
            "def normalize_ttl(seconds: int) -> int:\n    return max(0, seconds)\n",
        ),
        (
            "PRACTICE-CACHE-003",
            "def eviction_candidate(entries: list[tuple[str, int]]) -> str:\n"
            "    return min(entries, key=lambda entry: entry[1])[0]\n",
        ),
    ],
)
def test_materialize_and_grade_every_practice_task(
    tmp_path: Path, task_id: str, fixed_source: str
) -> None:
    pack = tmp_path / "pack"
    workspace = tmp_path / "workspace"
    compile_practice(SOURCE, pack)
    materialize(pack, task_id, workspace)

    assert grade(pack, task_id, workspace) != 0
    (workspace / "app.py").write_text(fixed_source, encoding="utf-8")
    assert grade(pack, task_id, workspace) == 0


def test_agent_view_rejects_policy_and_upstream_leaks() -> None:
    for value in (
        {"memory_condition": "required_constraint"},
        {"problem_statement": "See github.com/example/repo"},
        {"repository": "django/django"},
        {"policy": "MUST_USE"},
    ):
        with pytest.raises(CorpusError):
            assert_agent_view_safe(value, source="test")


def test_safe_relative_path_rejects_control_and_traversal_paths() -> None:
    for value in ("../secret", "/absolute", ".git/config", "a\\b", "a/../b"):
        with pytest.raises(CorpusError):
            safe_relative_path(value)
    assert safe_relative_path("tests/test_issue.py") == "tests/test_issue.py"


def test_agent_task_lines_are_canonical_json(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    compile_practice(SOURCE, pack)
    for line in (pack / "agent/tasks.jsonl").read_text(encoding="utf-8").splitlines():
        assert canonical_json_bytes(json.loads(line)).decode().rstrip("\n") == line


def test_source_rejects_unknown_supersession() -> None:
    source = load_practice_source(SOURCE)
    memories = [dict(memory) for memory in source.memories]
    memories[0]["supersedes"] = ["P01-M999"]

    with pytest.raises(CorpusError, match="supersedes unknown"):
        validate_practice_source(replace(source, memories=tuple(memories)))


def test_source_rejects_irrelevant_task_for_experienced_user() -> None:
    source = load_practice_source(SOURCE)
    tasks = [dict(task) for task in source.tasks]
    irrelevant = next(
        task for task in tasks if task["memory_condition"] == "irrelevant"
    )
    irrelevant["active_user_id"] = "P03"

    with pytest.raises(CorpusError, match="repository-experienced"):
        validate_practice_source(replace(source, tasks=tuple(tasks)))
