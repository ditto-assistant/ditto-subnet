from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
BACKEND_WORKFLOW = ROOT / ".github/workflows/platform-ci.yml"
DASHBOARD_WORKFLOW = ROOT / ".github/workflows/platform-dashboard-ci.yml"
SHARED_WORKFLOW = ROOT / ".github/workflows/platform-verify.yml"
RELEASE_WORKFLOW = ROOT / ".github/workflows/release.yml"
SHARED_WORKFLOW_USE = "./.github/workflows/platform-verify.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _triggers(workflow: dict) -> dict:
    # PyYAML 1.1 parses the unquoted YAML key `on` as boolean true.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    return triggers


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_backend_and_dashboard_workflows_own_disjoint_platform_paths() -> None:
    backend_triggers = _triggers(_load(BACKEND_WORKFLOW))
    dashboard_triggers = _triggers(_load(DASHBOARD_WORKFLOW))
    backend_paths = backend_triggers["pull_request"]["paths"]
    dashboard_paths = dashboard_triggers["pull_request"]["paths"]

    assert backend_paths[:5] == [
        "apps/platform/**",
        "!apps/platform/dashboard/**",
        "!apps/platform/faircopy.config.mjs",
        "!apps/platform/package.json",
        "!apps/platform/package-lock.json",
    ]
    assert {
        "apps/platform/dashboard/**",
        "apps/platform/faircopy.config.mjs",
        "apps/platform/package.json",
        "apps/platform/package-lock.json",
    } <= set(dashboard_paths)
    for paths in (backend_paths, dashboard_paths):
        assert "packages/ditto-screening-protocol/**" in paths
        assert ".github/workflows/platform-verify.yml" in paths
    for triggers in (backend_triggers, dashboard_triggers):
        assert "workflow_dispatch" in triggers
        assert "push" not in triggers


def test_shared_verification_deals_the_complete_suite_across_uniform_shards() -> None:
    test_job = _load(SHARED_WORKFLOW)["jobs"]["test"]
    shards = test_job["strategy"]["matrix"]["shard"]
    pytest_step = _step(test_job, "pytest")

    assert shards == list(range(8))
    # The count in DITTO_TEST_SHARD must equal the matrix width, or a slice
    # of the suite silently never runs.
    expected_shard = "${{ matrix.shard }}/" + str(len(shards))
    assert pytest_step["env"]["DITTO_TEST_SHARD"] == expected_shard
    assert pytest_step["run"] == "uv run --group browser pytest --durations=25"
    assert pytest_step["env"]["DITTO_REQUIRE_POSTGRES"] == "1"
    assert pytest_step["env"]["DITTO_REQUIRE_OBJECT_STORAGE"] == "1"
    assert pytest_step["env"]["DITTO_REQUIRE_BROWSER"] == "1"
    assert "if" not in _step(test_job, "Start MinIO")
    chromium = _step(
        test_job, "Install Chromium for the GitHub OAuth browser regression"
    )
    assert chromium["if"] == "matrix.shard == 0"


def test_shared_verification_keeps_static_database_and_security_gates() -> None:
    workflow = _load(SHARED_WORKFLOW)
    jobs = workflow["jobs"]
    static = jobs["static"]
    test_job = jobs["test"]

    assert static["if"] == "inputs.backend"
    assert _step(static, "ruff format check")["run"] == "uv run ruff format --check ."
    assert _step(static, "ruff check")["run"] == "uv run ruff check ."
    assert _step(static, "mypy")["run"] == "uv run mypy ditto/"
    assert jobs["security"]["if"] == "inputs.security"
    assert _step(jobs["security"], "Scan reachable Git history for secrets")
    assert test_job["services"]["postgres"]["image"] == "postgres:16-alpine"
    tune = _step(test_job, "Tune disposable Postgres")["run"]
    for setting in ("fsync", "synchronous_commit", "full_page_writes"):
        assert setting in tune
    assert "pg_reload_conf" in tune


def test_release_owns_shared_exact_source_component_gates() -> None:
    workflow = _load(RELEASE_WORKFLOW)
    plan = workflow["jobs"]["plan"]
    verify = workflow["jobs"]["verify-platform"]

    assert plan["outputs"]["platform_api"] == (
        "${{ steps.components.outputs.platform_api }}"
    )
    assert plan["outputs"]["platform_dashboard"] == (
        "${{ steps.components.outputs.platform_dashboard }}"
    )

    assert "needs.plan.outputs.platform == 'true'" in verify["if"]
    assert verify["uses"] == SHARED_WORKFLOW_USE
    assert verify["with"] == {
        "ref": "${{ github.sha }}",
        "backend": "${{ needs.plan.outputs.platform_api == 'true' }}",
        "dashboard": "${{ needs.plan.outputs.platform_dashboard == 'true' }}",
    }
    assert "steps" not in verify


def test_migration_order_sweeps_share_one_lane_and_checks_stay_isolated() -> None:
    workflow = _load(ROOT / ".github/workflows/platform-migration-order.yml")
    # A workflow-level group would put per-ref checks and sweeps in one lane.
    assert "concurrency" not in workflow
    check = workflow["jobs"]["migration-order"]
    sweep = workflow["jobs"]["recheck-open-prs"]

    # Push and a manual dispatch on main run the sweep under one constant group.
    # A branch dispatch (anti-copy refresh) must not: it would run that branch's
    # older copy of the sweep and could rewrite every open PR's status last.
    assert " ".join(sweep["if"].split()) == (
        "github.event_name == 'push' || "
        "(github.event_name == 'workflow_dispatch' && "
        "github.ref == 'refs/heads/main')"
    )
    assert sweep["concurrency"] == {
        "group": "platform-migration-order-main-sweep",
        "cancel-in-progress": False,
    }

    # PR, merge_group and dispatch checks are keyed by head/ref, never the sweep.
    assert check["if"] == "github.event_name != 'push'"
    assert check["concurrency"] == {
        "group": "platform-migration-order-${{ github.head_ref || github.ref }}",
        "cancel-in-progress": True,
    }
    assert "main-sweep" not in check["concurrency"]["group"]
