"""Provider migration must preserve executable validation and trust boundaries."""

import re
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ENTRYPOINTS = (
    "ci.yml",
    "backroom-ci.yml",
    "platform-ci.yml",
    "model-relay.yml",
    "conventional-pr.yml",
)
REUSABLES = ("platform-verify.yml", "root-verify.yml")
FALLBACK = (
    "github.event_name != 'pull_request' || "
    "github.event.pull_request.head.repo.full_name != github.repository"
)


def load(provider: str, name: str) -> dict:
    return yaml.load(
        (ROOT / provider / "workflows" / name).read_text(), Loader=yaml.BaseLoader
    )


@pytest.mark.parametrize("name", ENTRYPOINTS + REUSABLES)
def test_depot_keeps_the_current_executable_jobs(name: str) -> None:
    github_jobs = deepcopy(load(".github", name)["jobs"])
    depot_jobs = deepcopy(load(".depot", name)["jobs"])
    for job in github_jobs.values():
        condition = job.get("if", "")
        if condition == FALLBACK:
            del job["if"]
        elif condition.endswith(f" && ({FALLBACK})"):
            job["if"] = condition.removesuffix(f" && ({FALLBACK})")[1:-1]
        if "runs-on" in job:
            assert job["runs-on"] in {"ubuntu-latest", "ubuntu-24.04"}
            job["runs-on"] = "depot-ubuntu-24.04-4"
        if "uses" in job:
            job["uses"] = job["uses"].replace(
                "./.github/workflows/", "./.depot/workflows/"
            )
    assert depot_jobs == github_jobs


@pytest.mark.parametrize("name", ENTRYPOINTS)
def test_depot_retains_component_trigger_coverage_and_github_fallback(
    name: str,
) -> None:
    github = load(".github", name)
    depot = load(".depot", name)
    github_pr = github["on"]["pull_request"] or {}
    depot_pr = depot["on"]["pull_request"] or {}
    assert set(github_pr.get("paths", [])) <= set(depot_pr.get("paths", []))
    assert depot_pr.get("types") == github_pr.get("types")
    assert "push" not in depot["on"]
    for job in github["jobs"].values():
        assert FALLBACK in job["if"]


def test_depot_preview_retains_only_unprivileged_control_validation() -> None:
    github = load(".github", "preview.yml")
    depot = load(".depot", "preview.yml")
    assert set(depot["jobs"]) == {"plan", "cheatcodes"}
    for name, job in depot["jobs"].items():
        expected = deepcopy(github["jobs"][name])
        expected["runs-on"] = "depot-ubuntu-24.04-4"
        assert job == expected
    assert "dashboard-publish" in github["jobs"]


def test_depot_migration_owner_preserves_proof_and_excludes_the_main_sweep() -> None:
    github = load(".github", "platform-migration-order.yml")
    depot = load(".depot", "platform-migration-order.yml")
    assert set(depot["on"]) == {"pull_request"}
    assert set(depot["jobs"]) == {"migration-order"}
    expected = deepcopy(github["jobs"]["migration-order"])
    assert FALLBACK in expected["if"]
    expected["if"] = "github.event_name != 'push'"
    expected["runs-on"] = "depot-ubuntu-24.04-4"
    expected["steps"][1]["env"]["RUN_URL"] = (
        "${{ github.server_url }}/${{ github.repository }}/commit/"
        "${{ github.event.pull_request.head.sha || github.sha }}/checks"
    )
    assert depot["jobs"]["migration-order"] == expected
    main = github["jobs"]["recheck-open-prs"]
    assert "github.ref == 'refs/heads/main'" in main["if"]
    assert main["concurrency"]["cancel-in-progress"] == "false"


def test_depot_validation_has_no_production_authority_and_is_security_scanned() -> None:
    for path in (ROOT / ".depot/workflows").glob("*.yml"):
        text = path.read_text()
        assert not re.search(r"\b(?:secrets|environment):", text), path
        assert "${{ secrets." not in text, path
        workflow = load(".depot", path.name)
        assert (
            workflow.get("permissions", {}) == {"contents": "read"}
            or (
                path.name == "preview.yml"
                and workflow["permissions"]
                == {"contents": "read", "pull-requests": "read"}
            )
            or (path.name == "ci.yml" and "permissions" not in workflow)
        )
        for job in workflow["jobs"].values():
            assert "environment" not in job, path
            assert "id-token" not in job.get("permissions", {}), path
            permissions = job.get("permissions", {})
            if "write" in permissions.values():
                assert path.name == "platform-migration-order.yml"
                assert permissions == {"contents": "read", "statuses": "write"}
    security = (ROOT / ".github/scripts/check_workflow_security.py").read_text()
    assert 'Path(".depot/workflows")' in security
