"""Manual Depot fallback preserves validation without automatic PR spend."""

import os
import re
import subprocess
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
UTILITY_WORKFLOWS = ("preview.yml", "platform-migration-order.yml")


def load(provider: str, name: str) -> dict:
    return yaml.load(
        (ROOT / provider / "workflows" / name).read_text(), Loader=yaml.BaseLoader
    )


@pytest.mark.parametrize("name", ENTRYPOINTS + REUSABLES)
def test_depot_keeps_the_current_executable_jobs(name: str) -> None:
    github_jobs = deepcopy(load(".github", name)["jobs"])
    depot_jobs = deepcopy(load(".depot", name)["jobs"])
    for job in github_jobs.values():
        if "runs-on" in job:
            assert job["runs-on"] in {"ubuntu-latest", "ubuntu-24.04"}
            job["runs-on"] = "depot-ubuntu-24.04-4"
        if "uses" in job:
            job["uses"] = job["uses"].replace(
                "./.github/workflows/", "./.depot/workflows/"
            )
    assert depot_jobs == github_jobs


@pytest.mark.parametrize("name", ENTRYPOINTS + UTILITY_WORKFLOWS)
def test_github_owns_pr_validation_and_depot_is_manual_only(name: str) -> None:
    github = load(".github", name)
    depot = load(".depot", name)
    assert "pull_request" in github["on"]
    assert set(depot["on"]) == {"workflow_dispatch"}
    for job in github["jobs"].values():
        assert "head.repo.full_name != github.repository" not in job.get("if", "")


@pytest.mark.parametrize("name", REUSABLES)
def test_depot_reusable_verifiers_have_no_automatic_triggers(name: str) -> None:
    assert set(load(".depot", name)["on"]) == {"workflow_call"}


def test_manual_relay_fallback_does_not_skip_release_binary_validation() -> None:
    for provider in (".github", ".depot"):
        assert (
            "if"
            not in load(provider, "model-relay.yml")["jobs"]["static-release-build"]
        )


@pytest.mark.parametrize(
    ("event_title", "manual_titles", "api_exit", "expected"),
    (
        ("chore(ci): restore GitHub", "", 1, 0),
        ("invalid title", "", 0, 1),
        ("", "chore(ci): manual fallback", 0, 0),
        ("", "invalid title", 0, 1),
        ("", "", 0, 1),
        ("", "chore: one\nchore: two", 0, 1),
        ("", "chore: valid but API failed", 1, 1),
    ),
)
def test_title_validation_handles_manual_events_without_accepting_missing_proof(
    tmp_path: Path, event_title: str, manual_titles: str, api_exit: int, expected: int
) -> None:
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$MANUAL_TITLES"\nexit "$API_EXIT"\n')
    gh.chmod(0o755)
    script = load(".depot", "conventional-pr.yml")["jobs"]["title"]["steps"][0]["run"]
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "PR_TITLE": event_title,
            "MANUAL_TITLES": manual_titles,
            "API_EXIT": str(api_exit),
            "HEAD_SHA": "a" * 40,
            "GITHUB_REPOSITORY": "ditto-assistant/ditto-subnet",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr


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
    assert set(depot["on"]) == {"workflow_dispatch"}
    assert set(depot["jobs"]) == {"migration-order"}
    expected = deepcopy(github["jobs"]["migration-order"])
    assert expected["if"] == "github.event_name != 'push'"
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
