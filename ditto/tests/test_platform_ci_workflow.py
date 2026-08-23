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


def test_backend_workflow_owns_every_platform_coding_contract_vector() -> None:
    backend_paths = set(_triggers(_load(BACKEND_WORKFLOW))["pull_request"]["paths"])
    assert {
        "packages/dittobench-coding-contract/testdata/coding_contract_v1.json",
        "packages/dittobench-coding-contract/testdata/coding_artifact_capability_v1.json",
        "packages/dittobench-coding-contract/testdata/coding_catalog_v1.json",
        "packages/dittobench-coding-contract/testdata/coding_selection_v1.json",
        "packages/dittobench-coding-contract/testdata/coding_certification_v1.json",
        "packages/dittobench-coding-contract/testdata/coding_authoring_freeze_v1.json",
        "packages/dittobench-coding-contract/testdata/coding_grading_lease_v1.json",
        "packages/dittobench-coding-contract/testdata/coding_shadow_result_submission_v1.json",
        "packages/dittobench-coding-contract/testdata/coding_inference_policy_v1.json",
    } <= backend_paths
    assert (
        "packages/dittobench-coding-contract/testdata/coding_inference_miner_v1.json"
        not in backend_paths
    )
    assert (
        "packages/dittobench-coding-contract/testdata/coding_execution_plan_v1.json"
        not in backend_paths
    )


def test_path_gated_callers_select_the_shared_exact_source_gates() -> None:
    backend = _load(BACKEND_WORKFLOW)["jobs"]["verify"]
    dashboard = _load(DASHBOARD_WORKFLOW)["jobs"]["verify"]

    assert backend["uses"] == SHARED_WORKFLOW_USE
    assert backend["with"] == {
        "ref": "${{ github.sha }}",
        "security": True,
        "backend": True,
    }
    assert dashboard["uses"] == SHARED_WORKFLOW_USE
    assert dashboard["with"] == {
        "ref": "${{ github.sha }}",
        "security": True,
        "dashboard": True,
    }


def test_shared_verification_shards_the_complete_suite_without_deselection() -> None:
    workflow = _load(SHARED_WORKFLOW)
    test_job = workflow["jobs"]["test"]
    shards = {
        entry["suite"]: (entry["pytest_args"], entry["object_storage"])
        for entry in test_job["strategy"]["matrix"]["include"]
    }

    assert shards == {
        "public-validator-endpoints": (
            "ditto/tests/api_server/endpoints/test_public.py "
            "ditto/tests/api_server/endpoints/test_validator.py",
            False,
        ),
        "other-endpoints": (
            "ditto/tests/api_server/endpoints "
            "--ignore=ditto/tests/api_server/endpoints/test_public.py "
            "--ignore=ditto/tests/api_server/endpoints/test_validator.py",
            False,
        ),
        "api-server": (
            "ditto/tests/api_server --ignore=ditto/tests/api_server/endpoints",
            False,
        ),
        "data-contract-integration": ("--ignore=ditto/tests/api_server", True),
    }
    pytest_step = _step(test_job, "pytest")
    assert pytest_step["run"] == "uv run pytest ${{ matrix.pytest_args }}"
    assert pytest_step["env"]["DITTO_REQUIRE_POSTGRES"] == "1"
    assert pytest_step["env"]["DITTO_REQUIRE_OBJECT_STORAGE"] == "1"
    assert _step(test_job, "Start MinIO")["if"] == "matrix.object_storage"


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


def test_shared_dashboard_preserves_copy_check_test_and_build() -> None:
    dashboard = _load(SHARED_WORKFLOW)["jobs"]["dashboard"]

    assert dashboard["if"] == "inputs.dashboard"
    assert _step(dashboard, "faircopy")["run"] == (
        "npm run lint:copy -- --format github"
    )
    assert [_step(dashboard, name)["run"] for name in ("Check", "Test", "Build")] == [
        "npm run check",
        "npm test",
        "npm run build",
    ]


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


def test_migration_order_retries_status_publish_without_failing_a_clean_check() -> None:
    text = (ROOT / ".github/workflows/platform-migration-order.yml").read_text()
    assert "max_attempts=6" in text
    assert "leaving that context pending" in text
    assert 'exit "$exit_code"' in text
