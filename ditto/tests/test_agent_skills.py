from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
LOOKUP = (
    ROOT
    / ".agents"
    / "skills"
    / "ditto-subnet-context"
    / "scripts"
    / "lookup-context.py"
)
SPEC = importlib.util.spec_from_file_location("lookup_context", LOOKUP)
assert SPEC is not None and SPEC.loader is not None
lookup_context = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lookup_context)


def lookup(query: str, maximum: int = 3) -> list[dict[str, object]]:
    completed = subprocess.run(
        [
            sys.executable,
            str(LOOKUP),
            "--json",
            "--max-topics",
            str(maximum),
            query,
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_context_index_paths_exist() -> None:
    subprocess.run(
        [sys.executable, str(LOOKUP), "--check"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_context_index_rejects_paths_outside_repository(capsys) -> None:
    for escaped in ("/tmp", ".."):
        data = {
            "topics": [
                {
                    "id": "escape",
                    "owns": [escaped],
                    "read": [],
                }
            ]
        }
        assert lookup_context.check_index(data) == 1
        assert "path escapes repository" in capsys.readouterr().err


def test_worktree_parser_preserves_spaces() -> None:
    script = (
        ROOT
        / ".agents"
        / "skills"
        / "ditto-subnet-worktree"
        / "scripts"
        / "create-worktree.sh"
    ).read_text()
    assert 'primary_root="${line#worktree }"' in script
    assert "awk 'NR == 1 {print $2}'" not in script


def test_platform_backroom_query_routes_to_both_owners() -> None:
    topic_ids = {
        str(topic["id"])
        for topic in lookup("Platform API migration changes Backroom admin behavior")
    }
    assert {"platform-api", "backroom"} <= topic_ids


def test_targon_query_routes_to_capacity_and_release() -> None:
    topic_ids = {
        str(topic["id"])
        for topic in lookup(
            "scale Targon screeners to zero with GCE fallback and deploy the "
            "trusted builder",
            maximum=2,
        )
    }
    assert topic_ids == {"screener-capacity", "release-delivery"}


def test_typo_heavy_screener_builder_query_keeps_the_capacity_owner_first() -> None:
    topic_ids = [str(topic["id"]) for topic in lookup("screeners/buildres")]
    assert topic_ids == ["screener-capacity"]


def test_targon_scale_to_zero_does_not_route_to_worktrees() -> None:
    topic_ids = {
        str(topic["id"])
        for topic in lookup(
            "Scale screeners to zero and use Targon first with bounded GCE fallback"
        )
    }
    assert "screener-capacity" in topic_ids
    assert "worktrees" not in topic_ids


def test_short_keywords_do_not_match_inside_unrelated_tokens() -> None:
    topic_ids = {str(topic["id"]) for topic in lookup("preview buildres", maximum=3)}
    assert "worktrees" not in topic_ids
    assert "platform-dashboard" not in topic_ids


def test_specialized_skills_pass_raw_task_text_to_context_lookup() -> None:
    for skill in (
        "ditto-subnet-platform",
        "ditto-subnet-benchmark",
        "ditto-subnet-release-ops",
    ):
        body = (ROOT / ".agents" / "skills" / skill / "SKILL.md").read_text()
        assert "--max-topics" in body
        assert '"$ARGUMENTS"' in body
    platform = (
        ROOT / ".agents" / "skills" / "ditto-subnet-platform" / "SKILL.md"
    ).read_text()
    assert "platform api database dashboard backroom $ARGUMENTS" not in platform


def test_longmemeval_query_preserves_research_scoring_boundary() -> None:
    topic_ids = [
        str(topic["id"])
        for topic in lookup(
            "run v8 agents against LongMemEval without changing scoring", maximum=2
        )
    ]
    assert topic_ids == ["dittobench-scoring", "research-adapters"]
