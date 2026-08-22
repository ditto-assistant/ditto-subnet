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


def _tracked_skill_names(prefix: str) -> set[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--", prefix],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    names: set[str] = set()
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        parts = Path(raw.decode()).parts
        if len(parts) >= 3 and parts[1] == "skills":
            names.add(parts[2])
    return names


def test_every_skill_is_available_to_agents_and_claude() -> None:
    agents = _tracked_skill_names(".agents/skills")
    claude = _tracked_skill_names(".claude/skills")
    assert agents, "expected at least one tracked .agents skill"
    missing_from_claude = sorted(agents - claude)
    missing_from_agents = sorted(claude - agents)
    assert not missing_from_claude, (
        "skills must also exist under .claude/skills "
        f"(symlink or Claude-specific tree): {missing_from_claude}"
    )
    assert not missing_from_agents, (
        f"skills must also exist under .agents/skills: {missing_from_agents}"
    )


def test_shared_claude_skill_symlinks_point_at_agents_tree() -> None:
    agents_root = (ROOT / ".agents" / "skills").resolve()
    claude_root = ROOT / ".claude" / "skills"
    for path in sorted(claude_root.iterdir()):
        if not path.is_symlink():
            assert (path / "SKILL.md").is_file(), path
            assert (agents_root / path.name / "SKILL.md").is_file(), path.name
            continue
        target = (path.parent / path.readlink()).resolve()
        expected = agents_root / path.name
        assert target == expected, f"{path} -> {target}, expected {expected}"
        assert (target / "SKILL.md").is_file(), target


_COMPONENT_SKILL_PREFIXES = (
    "apps",
    "services",
    "packages",
    "research",
    "infra",
    "workers",
    "miners",
    "ditto",
)


def test_component_local_skills_are_symlinks_to_agents_tree() -> None:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--", *_COMPONENT_SKILL_PREFIXES],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    agents_root = (ROOT / ".agents" / "skills").resolve()
    owned_files: list[str] = []
    seen_skill_dirs: set[Path] = set()

    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        rel = Path(raw.decode())
        parts = rel.parts
        try:
            agents_idx = parts.index(".agents")
        except ValueError:
            continue
        if agents_idx + 2 >= len(parts) or parts[agents_idx + 1] != "skills":
            continue
        skill_rel = Path(*parts[: agents_idx + 3])
        skill_path = ROOT / skill_rel
        if not skill_path.is_symlink():
            owned_files.append(str(rel))
            continue
        if skill_rel in seen_skill_dirs:
            continue
        seen_skill_dirs.add(skill_rel)
        target = (skill_path.parent / skill_path.readlink()).resolve()
        expected = agents_root / skill_path.name
        assert target == expected, f"{skill_rel} -> {target}, expected {expected}"
        assert (target / "SKILL.md").is_file(), expected

    assert not owned_files, (
        "component-local .agents/skills trees may only symlink to "
        f"repo-root .agents/skills/<name>: {owned_files}"
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


def test_wandb_query_routes_to_operations_without_hijacking_generic_run_verbs() -> None:
    topic_ids = [
        str(topic["id"])
        for topic in lookup("compare wandb run history across validator nodes")
    ]
    assert topic_ids[0] == "wandb-operations"

    generic = {
        str(topic["id"])
        for topic in lookup("run the validator scoring job again", maximum=3)
    }
    assert "wandb-operations" not in generic


def test_backroom_review_skill_covers_both_courts() -> None:
    skill = ROOT / ".agents" / "skills" / "backroom-review"
    body = (skill / "SKILL.md").read_text()
    metadata = (skill / "agents" / "openai.yaml").read_text()
    agents_root = ROOT / ".agents" / "skills"
    claude_link = ROOT / ".claude" / "skills" / "backroom-review"

    assert "https://backroom.dittobench.ai/mcp" in body
    assert "https://backroom.dittobench.ai/mcp" in metadata
    assert 'value: "sn118-backroom"' in metadata
    assert "backroom.heyditto.ai" not in body + metadata
    assert "preview_screening_quarantine_batch" in body
    assert "execute_screening_quarantine_batch" in body
    assert "confirmed: true" in body
    assert "Agent-returned tool-call traces are not proof" in body
    assert "search_ath_precedents" in body
    assert "open_ath_review" in body
    assert "resolve_ath_review" in body
    assert (skill / "scripts" / "prepare_artifact.py").is_file()
    assert (skill / "scripts" / "search-precedents.py").is_file()
    assert (skill / "references" / "review-rules.md").is_file()
    assert (skill / "references" / "review-bar.md").is_file()
    assert claude_link.is_symlink()
    assert (claude_link.parent / claude_link.readlink()).resolve() == skill.resolve()
    assert not (agents_root / "backroom-board-review").exists()
    assert not (agents_root / "backroom-submission-triage").exists()


def test_quarantine_and_ath_queries_route_to_backroom_review() -> None:
    for query in (
        "review the screening quarantine queue and release false positives",
        "ATH board review of high-score family compiler agents",
    ):
        topic_ids = [str(topic["id"]) for topic in lookup(query)]
        assert topic_ids[0] == "backroom-review", query


def test_mine_skill_applies_operator_review_bar_locally() -> None:
    skill = ROOT / ".agents" / "skills" / "mine"
    body = (skill / "SKILL.md").read_text()
    metadata = (skill / "agents" / "openai.yaml").read_text()
    review_root = ROOT / ".agents" / "skills" / "backroom-review"

    assert "review-bar.md" in body
    assert "review-rules.md" in body
    assert "techniques.md" in body
    assert "search-precedents.py" in body
    assert "two-limb" in body
    assert "production-engine" in body
    assert "Would reject" in body
    assert "Do not run `full` as certification, package, or upload" in body
    assert "execute_screening_quarantine_batch" not in body
    assert "open_ath_review" not in body
    assert "https://backroom.dittobench.ai/mcp" not in body
    assert "https://backroom.dittobench.ai/mcp" not in metadata
    assert (review_root / "references" / "review-bar.md").is_file()
    assert (review_root / "references" / "review-rules.md").is_file()
    assert (review_root / "references" / "techniques.md").is_file()
    assert (review_root / "scripts" / "search-precedents.py").is_file()


def test_miner_pre_submit_review_query_stays_on_mine() -> None:
    topic_ids = [
        str(topic["id"])
        for topic in lookup(
            "practice the starter kit and upload after reviewing the served path"
        )
    ]
    assert topic_ids[0] == "mine"
