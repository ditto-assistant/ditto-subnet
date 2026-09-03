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


def topic_list(topic: dict[str, object], key: str) -> list[str]:
    values = topic.get(key, [])
    assert isinstance(values, list)
    return [str(value) for value in values]


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


def test_context_index_requires_installed_skills_to_be_routed(capsys) -> None:
    data = lookup_context.load_index()
    data = {**data, "topics": [dict(topic) for topic in data["topics"]]}
    data["topics"][0] = {**data["topics"][0], "skills": []}
    # Drop every skills field so coverage must fail closed.
    for topic in data["topics"]:
        topic["skills"] = []
    assert lookup_context.check_index(data, cover_installed_skills=True) == 1
    assert "not referenced by any topic.skills" in capsys.readouterr().err


def test_context_index_rejects_unknown_related_topic(capsys) -> None:
    data = {
        "topics": [
            {
                "id": "only",
                "owns": [],
                "read": [],
                "related": ["missing"],
            }
        ]
    }
    assert lookup_context.check_index(data) == 1
    assert "unknown topic id" in capsys.readouterr().err


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


def test_hippius_canary_operator_query_routes_to_operator_contract() -> None:
    topic_ids = [
        str(topic["id"])
        for topic in lookup("run Hippius canary with protected helper executable")
    ]
    assert topic_ids[0] == "coding-hippius-canary-operator"


def test_hippius_token_expiry_query_routes_to_lifecycle_skill() -> None:
    topics = lookup("audit Hippius token age before expiry and rotate credentials")
    infra_cloud = next(topic for topic in topics if topic["id"] == "infra-cloud")
    assert "hippius-token-lifecycle" in topic_list(infra_cloud, "skills")


def test_hippius_canary_helper_query_routes_to_proxy_packaging() -> None:
    topic_ids = [
        str(topic["id"])
        for topic in lookup("package canary helpers with SO_PEERCRED backend sockets")
    ]
    assert topic_ids[0] == "coding-hippius-canary-helpers"


def test_hippius_canary_unwrap_query_routes_to_isolated_service() -> None:
    topic_ids = [
        str(topic["id"])
        for topic in lookup("prepare exact Hippius canary unwrap authority")
    ]
    assert topic_ids[0] == "coding-hippius-canary-unwrap"


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


def test_generic_evaluate_does_not_route_to_mine() -> None:
    topic_ids = [
        str(topic["id"])
        for topic in lookup(
            "evaluate and improve agent skills for pulling the right context"
        )
    ]
    assert topic_ids[0] == "agent-skills"
    assert "mine" not in topic_ids


def test_context_index_and_lookup_query_routes_to_agent_skills() -> None:
    topic_ids = [
        str(topic["id"])
        for topic in lookup("update the context index and lookup-context.py routing")
    ]
    assert topic_ids[0] == "agent-skills"


def test_bench_version_bump_query_selects_the_bump_skill() -> None:
    for query in (
        "bench version bump",
        "ship bench_version v13 across every layer",
        "strand a new bench version with Literal[9]",
    ):
        topics = lookup(query)
        topic_ids = [str(topic["id"]) for topic in topics]
        assert topic_ids[0] == "bench-version-bump", query
        assert "ditto-subnet-bench-version-bump" in topic_list(topics[0], "skills")


def test_longmem_confirmation_rollout_query_selects_the_rollout_skill() -> None:
    topic_ids = [
        str(topic["id"])
        for topic in lookup("LongMem confirmation rollout fail-closed canary")
    ]
    assert topic_ids[0] == "longmem-confirmation"


def test_dimension_execution_query_selects_longmem_confirmation() -> None:
    topic_ids = [
        str(topic["id"])
        for topic in lookup("dimension_execution failure on confirmation ticket")
    ]
    assert topic_ids[0] == "longmem-confirmation"


def test_production_postgres_explain_routes_to_gcloud_readonly() -> None:
    topics = lookup("query production postgres EXPLAIN ANALYZE")
    topic_ids = [str(topic["id"]) for topic in topics]
    assert topic_ids[0] == "prod-readonly"
    assert "gcloud-ditto-readonly" in topic_list(topics[0], "skills")


def test_targon_kaniko_log_query_prefers_readonly_skill_over_capacity() -> None:
    topic_ids = [str(topic["id"]) for topic in lookup("Targon kaniko builder logs")]
    assert topic_ids[0] == "prod-readonly"
    assert "screener-capacity" in topic_ids or len(topic_ids) == 1


def test_platform_disk_full_routes_to_host_disk_topic() -> None:
    topics = lookup("platform deploy no space left on device")
    topic_ids = [str(topic["id"]) for topic in topics]
    assert topic_ids[0] == "platform-host-disk"
    named = {name for topic in topics for name in topic_list(topic, "skills")}
    assert "gcloud-ditto-readonly" in named
    assert "ditto-subnet-release-ops" in named


def test_preview_compose_stack_does_not_route_to_worktrees() -> None:
    topic_ids = {
        str(topic["id"]) for topic in lookup("preview compose stack attach-prod-api")
    }
    assert "preview-channels" in topic_ids
    assert "worktrees" not in topic_ids
    assert "platform-api" not in topic_ids


def test_public_canary_inference_settlement_routes_to_request_ledger() -> None:
    topic_ids = [
        str(topic["id"])
        for topic in lookup("claimed-lease public-canary inference settlement")
    ]
    assert topic_ids[0] == "coding-inference-requests"


def test_certified_canary_settlement_bind_routes_to_request_ledger() -> None:
    topic_ids = [
        str(topic["id"]) for topic in lookup("certified canary settlement bind")
    ]
    assert topic_ids[0] == "coding-inference-requests"


def test_platform_api_review_does_not_select_ath_review() -> None:
    topic_ids = {str(topic["id"]) for topic in lookup("review the platform API change")}
    assert "platform-api" in topic_ids
    assert "backroom-review" not in topic_ids


def test_dashboard_profile_query_does_not_select_runtime_profiling() -> None:
    topic_ids = {str(topic["id"]) for topic in lookup("profile the dashboard UI")}
    assert "platform-dashboard" in topic_ids
    assert "runtime-profiling" not in topic_ids


def test_workers_screener_query_owns_the_worker_tree() -> None:
    topics = lookup("workers/screener policy gate")
    assert str(topics[0]["id"]) == "screener-worker"
    assert "workers/screener" in topic_list(topics[0], "owns")
    reads = topic_list(topics[0], "read")
    assert any(path.startswith("workers/screener/") for path in reads)
    assert not any("screener-orchestrator" in path for path in reads)


def test_quarantine_false_positive_stays_on_backroom_review() -> None:
    topic_ids = [
        str(topic["id"])
        for topic in lookup("review screening quarantine false positive")
    ]
    assert topic_ids[0] == "backroom-review"


def test_selected_topics_name_specialized_skills() -> None:
    topics = lookup("Platform API migration changes Backroom admin behavior")
    named = {name for topic in topics for name in topic_list(topic, "skills")}
    assert "ditto-subnet-platform" in named


def test_fuzzy_token_match_allows_plurals_but_rejects_substrings() -> None:
    assert lookup_context.fuzzy_token_match("screener", "screeners")
    assert lookup_context.fuzzy_token_match("builder", "buildres")
    assert not lookup_context.fuzzy_token_match("review", "preview")
    assert not lookup_context.fuzzy_token_match("api", "capital")


def test_partial_keyword_overlap_requires_two_tokens() -> None:
    assert lookup_context.keyword_overlap_score({"review"}, ("board", "review")) == 0
    assert (
        lookup_context.keyword_overlap_score(
            {"kaniko", "builder", "logs"}, ("kaniko", "logs")
        )
        == 6
    )


def test_lookup_explain_lists_topic_scores() -> None:
    completed = subprocess.run(
        [sys.executable, str(LOOKUP), "--explain", "bench version bump"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    first = completed.stdout.splitlines()[0]
    assert first.endswith("bench-version-bump")
    assert first.strip().split()[0].isdigit()
