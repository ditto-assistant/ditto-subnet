from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location(
    "release_plan", ROOT / "scripts" / "release-plan.py"
)
assert SPEC is not None and SPEC.loader is not None
release_plan = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_plan
SPEC.loader.exec_module(release_plan)


@pytest.fixture(scope="module")
def components():
    return release_plan.load_components(ROOT / "release" / "components.toml")


@pytest.fixture(scope="module")
def ignored_paths():
    return release_plan.load_ignored_paths(ROOT / "release" / "components.toml")


def selected(components, ignored_paths, *paths: str) -> set[str]:
    plan = release_plan.select_components(components, paths, ignored_paths)
    return {name for name, enabled in plan.items() if enabled}


def test_miner_only_change_does_not_release_validator_stack(
    components, ignored_paths
) -> None:
    assert selected(
        components, ignored_paths, "ditto/miner_cli/commands/upload.py"
    ) == {"miner_cli"}


def test_validator_change_propagates_to_stack(components, ignored_paths) -> None:
    assert selected(components, ignored_paths, "ditto/validator/worker.py") == {
        "validator",
        "validator_stack",
    }


def test_sandbox_change_propagates_to_stack(components, ignored_paths) -> None:
    assert selected(
        components, ignored_paths, "scripts/sandbox-docker-entrypoint.sh"
    ) == {
        "sandbox_docker",
        "validator_stack",
    }


def test_dittobench_change_propagates_to_stack(components, ignored_paths) -> None:
    assert selected(
        components,
        ignored_paths,
        "services/dittobench-api/cmd/dittobench-api/main.go",
    ) == {
        "dittobench_api",
        "validator_stack",
    }


@pytest.mark.parametrize(
    "path",
    [
        "services/dittobench-api/Dockerfile.egress-proxy",
        "services/dittobench-api/integrations/longmemeval/longmemeval_adapter.py",
        "services/dittobench-api/scripts/calibrate.sh",
        "services/dittobench-api/calibration/token-efficiency-v5/contract.json",
    ],
)
def test_all_dittobench_surfaces_release(components, ignored_paths, path: str) -> None:
    assert selected(components, ignored_paths, path) == {
        "dittobench_api",
        "validator_stack",
    }


def test_datagen_change_rebuilds_scorer_and_validator_stack(
    components, ignored_paths
) -> None:
    assert selected(
        components, ignored_paths, "research/dittobench-datagen/grade/grade.go"
    ) == {
        "dittobench_datagen",
        "dittobench_api",
        "validator_stack",
    }


def test_platform_change_does_not_release_validator_stack(
    components, ignored_paths
) -> None:
    assert selected(
        components, ignored_paths, "apps/platform/ditto/api_server/factory.py"
    ) == {"platform", "backroom"}


def test_screener_change_does_not_release_validator_stack(
    components, ignored_paths
) -> None:
    assert selected(
        components, ignored_paths, "workers/screener/ditto_screener/worker.py"
    ) == {"screener", "screener_orchestrator"}


def test_orchestrator_change_is_isolated_from_validator_release(
    components, ignored_paths
) -> None:
    assert selected(
        components,
        ignored_paths,
        "services/screener-orchestrator/screener_capacity/controller.py",
    ) == {"screener_orchestrator"}


def test_screening_contract_change_propagates_to_every_consumer(
    components, ignored_paths
) -> None:
    assert selected(
        components,
        ignored_paths,
        "packages/ditto-screening-protocol/ditto_screening_protocol/models.py",
    ) == {
        "screening_protocol",
        "miner_cli",
        "validator",
        "validator_stack",
        "platform",
        "backroom",
        "screener",
        "screener_orchestrator",
    }


def test_shared_contract_change_releases_both_surfaces(
    components, ignored_paths
) -> None:
    assert selected(components, ignored_paths, "ditto/api_models/upload.py") == {
        "miner_cli",
        "validator",
        "validator_stack",
    }


@pytest.mark.parametrize(
    "path",
    [
        "docs/MINER.md",
        "services/dittobench-api/docs/BASELINES.md",
        "ditto/tests/miner_cli/test_api_client.py",
        ".github/workflows/ci.yml",
    ],
)
def test_non_runtime_changes_do_not_release(
    components, ignored_paths, path: str
) -> None:
    assert selected(components, ignored_paths, path) == set()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("ditto/system_health.py", {"validator", "validator_stack"}),
        ("Dockerfile.pylon", {"validator_stack"}),
        (".dockerignore", {"validator_stack"}),
    ],
)
def test_live_runtime_inputs_are_mapped(
    components, ignored_paths, path: str, expected: set[str]
) -> None:
    assert selected(components, ignored_paths, path) == expected


def test_unmapped_change_fails_closed(components, ignored_paths) -> None:
    with pytest.raises(ValueError, match="not mapped"):
        selected(components, ignored_paths, "new-runtime-entrypoint.sh")


def test_git_diff_includes_type_changes(monkeypatch) -> None:
    captured: list[str] = []

    def run(command, **kwargs):
        assert kwargs == {"check": True, "capture_output": True}
        captured.extend(command)
        return subprocess.CompletedProcess(command, 0, stdout=b"")

    monkeypatch.setattr(release_plan.subprocess, "run", run)
    release_plan.git_changed_paths("base", "head")

    assert "--diff-filter=ACDMRT" in captured


def test_every_tracked_file_has_a_release_owner_or_explicit_ignore(
    components, ignored_paths
) -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    paths = tuple(
        path.decode("utf-8", errors="strict") for path in tracked.split(b"\0") if path
    )

    # The assertion is the absence of release-plan's fail-closed exception.
    release_plan.select_components(components, paths, ignored_paths)
