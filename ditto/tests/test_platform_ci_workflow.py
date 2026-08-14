from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
BACKEND_WORKFLOW = ROOT / ".github/workflows/platform-ci.yml"
DASHBOARD_WORKFLOW = ROOT / ".github/workflows/platform-dashboard-ci.yml"
RELEASE_WORKFLOW = ROOT / ".github/workflows/release.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _triggers(workflow: dict) -> dict:
    # PyYAML 1.1 parses the unquoted YAML key `on` as boolean true.
    return workflow[True]


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
    assert "packages/ditto-screening-protocol/**" in backend_paths
    assert "packages/ditto-screening-protocol/**" in dashboard_paths
    assert backend_triggers["push"]["paths"] == backend_paths
    assert dashboard_triggers["push"]["paths"] == dashboard_paths


def test_backend_ci_shards_the_complete_platform_suite_without_deselection() -> None:
    workflow = _load(BACKEND_WORKFLOW)
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


def test_backend_ci_keeps_static_and_database_safety_gates() -> None:
    workflow = _load(BACKEND_WORKFLOW)
    static = workflow["jobs"]["static"]
    test_job = workflow["jobs"]["test"]

    assert _step(static, "ruff format check")["run"] == "uv run ruff format --check ."
    assert _step(static, "ruff check")["run"] == "uv run ruff check ."
    assert _step(static, "mypy")["run"] == "uv run mypy ditto/"
    assert _step(static, "Scan reachable Git history for secrets")
    assert test_job["services"]["postgres"]["image"] == "postgres:16-alpine"
    tune = _step(test_job, "Tune disposable Postgres")["run"]
    for setting in ("fsync", "synchronous_commit", "full_page_writes"):
        assert setting in tune
    assert "pg_reload_conf" in tune


def test_dashboard_ci_preserves_copy_check_test_and_build() -> None:
    workflow = _load(DASHBOARD_WORKFLOW)
    jobs = workflow["jobs"]

    assert _step(jobs["copy-lint"], "faircopy")["run"] == (
        "npm run lint:copy -- --format github"
    )
    assert _step(jobs["copy-lint"], "Scan reachable Git history for secrets")
    dashboard = jobs["dashboard"]
    assert [_step(dashboard, name)["run"] for name in ("Check", "Test", "Build")] == [
        "npm run check",
        "npm test",
        "npm run build",
    ]


def test_release_still_runs_the_unsharded_full_platform_gate() -> None:
    workflow = _load(RELEASE_WORKFLOW)
    verify = workflow["jobs"]["verify-platform"]
    gate = _step(verify, "Gate Platform release on exact merge source")

    assert "needs.plan.outputs.platform == 'true'" in verify["if"]
    assert "uv run pytest" in gate["run"].splitlines()
