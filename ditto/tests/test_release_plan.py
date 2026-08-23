from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

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


def root_verification(components, ignored_paths, *paths: str) -> str:
    plan = release_plan.select_components(components, paths, ignored_paths)
    return release_plan.select_root_verification(components, plan, paths)


def test_miner_only_change_does_not_release_validator_stack(
    components, ignored_paths
) -> None:
    assert selected(
        components, ignored_paths, "ditto/miner_cli/commands/upload.py"
    ) == {"miner_cli"}


def test_starter_kit_change_is_an_isolated_miner_release(
    components, ignored_paths
) -> None:
    assert selected(
        components, ignored_paths, "miners/dittobench-starter-kit/src/baseline.rs"
    ) == {"miner_starter_kit"}


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


def test_dittobench_workflow_is_release_owned(components, ignored_paths) -> None:
    assert selected(components, ignored_paths, ".github/workflows/dittobench.yml") == {
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


def test_coding_datagen_change_is_shadow_only(components, ignored_paths) -> None:
    assert selected(
        components,
        ignored_paths,
        "research/dittobench-coding-datagen/src/dittobench_coding_datagen/compiler.py",
    ) == {
        "dittobench_coding_datagen",
        "dittobench_coding_starter_kit",
    }
    assert (
        root_verification(
            components,
            ignored_paths,
            "research/dittobench-coding-datagen/src/dittobench_coding_datagen/compiler.py",
        )
        == "none"
    )


def test_coding_datagen_workflow_is_release_owned(components, ignored_paths) -> None:
    assert selected(
        components, ignored_paths, ".github/workflows/coding-datagen-ci.yml"
    ) == {
        "dittobench_coding_datagen",
        "dittobench_coding_starter_kit",
    }


def test_coding_starter_kit_change_is_shadow_only(components, ignored_paths) -> None:
    path = "miners/dittobench-coding-starter-kit/src/agent.rs"
    assert selected(components, ignored_paths, path) == {
        "dittobench_coding_starter_kit"
    }
    assert root_verification(components, ignored_paths, path) == "none"


def test_coding_starter_workflow_is_release_owned(components, ignored_paths) -> None:
    assert selected(
        components,
        ignored_paths,
        ".github/workflows/coding-starter-kit-ci.yml",
    ) == {"dittobench_coding_starter_kit"}


def test_coding_starter_e2e_script_is_release_owned(components, ignored_paths) -> None:
    assert selected(
        components,
        ignored_paths,
        "scripts/test-coding-starter-practice-e2e.sh",
    ) == {"dittobench_coding_starter_kit"}


def test_shared_coding_memory_vector_selects_public_consumers(
    components, ignored_paths
) -> None:
    selected_components = selected(
        components,
        ignored_paths,
        "packages/dittobench-coding-contract/testdata/coding_memory_v1.json",
    )
    assert {
        "dittobench_coding_datagen",
        "dittobench_coding_starter_kit",
    } <= selected_components


def test_coding_inference_miner_vector_excludes_platform_authority(
    components, ignored_paths
) -> None:
    path = "packages/dittobench-coding-contract/testdata/coding_inference_miner_v1.json"
    assert selected(components, ignored_paths, path) == {
        "dittobench_api",
        "dittobench_coding_datagen",
        "dittobench_coding_starter_kit",
        "validator",
        "validator_stack",
    }
    assert root_verification(components, ignored_paths, path) == "full"


def test_coding_execution_plan_vector_selects_delivery_consumers(
    components, ignored_paths
) -> None:
    path = "packages/dittobench-coding-contract/testdata/coding_execution_plan_v1.json"
    assert selected(components, ignored_paths, path) == {
        "dittobench_api",
        "dittobench_coding_datagen",
        "dittobench_coding_starter_kit",
        "platform_api",
        "platform",
        "backroom",
        "validator",
        "validator_stack",
    }
    assert root_verification(components, ignored_paths, path) == "full"


def test_coding_execution_delivery_generator_selects_every_consumer(
    components, ignored_paths
) -> None:
    path = "packages/dittobench-coding-contract/generate_execution_delivery_vectors.py"
    assert selected(components, ignored_paths, path) == {
        "backroom",
        "dittobench_api",
        "dittobench_coding_datagen",
        "dittobench_coding_starter_kit",
        "platform",
        "platform_api",
        "validator",
        "validator_stack",
    }
    assert root_verification(components, ignored_paths, path) == "full"


def test_coding_contract_models_select_scorer_and_validator_stack(
    components, ignored_paths
) -> None:
    assert selected(
        components,
        ignored_paths,
        "ditto/api_models/coding.py",
        "services/dittobench-api/internal/codingcontract/types.go",
    ) == {"dittobench_api", "validator", "validator_stack"}


@pytest.mark.parametrize(
    "path",
    [
        "ditto/api_models/coding_inference.py",
        "ditto/api_models/coding_inference_grants.py",
    ],
)
def test_validator_coding_inference_models_are_release_owned(
    components, ignored_paths, path: str
) -> None:
    assert selected(components, ignored_paths, path) == {
        "validator",
        "validator_stack",
    }
    assert root_verification(components, ignored_paths, path) == "full"


@pytest.mark.parametrize(
    "path",
    [
        "services/dittobench-api/internal/codingrunner/session.go",
        "services/dittobench-api/internal/codinggrader/grader.go",
        "services/dittobench-api/internal/codingexecutor/executor.go",
        "services/dittobench-api/internal/codingrelay/relay.go",
    ],
)
def test_shadow_coding_execution_selects_only_scorer_stack(
    components, ignored_paths, path: str
) -> None:
    assert selected(
        components,
        ignored_paths,
        path,
    ) == {"dittobench_api", "validator_stack"}


@pytest.mark.parametrize(
    "path",
    [
        "packages/dittobench-coding-contract/testdata/coding_contract_v1.json",
        "packages/dittobench-coding-contract/testdata/coding_artifact_capability_v1.json",
        "packages/dittobench-coding-contract/testdata/coding_catalog_v1.json",
        "packages/dittobench-coding-contract/testdata/coding_selection_v1.json",
        "packages/dittobench-coding-contract/testdata/coding_certification_v1.json",
        "packages/dittobench-coding-contract/testdata/coding_authoring_freeze_v1.json",
        "packages/dittobench-coding-contract/testdata/coding_grading_lease_v1.json",
        "packages/dittobench-coding-contract/testdata/coding_shadow_result_submission_v1.json",
        "packages/dittobench-coding-contract/testdata/coding_inference_policy_v1.json",
    ],
)
def test_shared_coding_contract_vectors_select_every_consumer(
    components, ignored_paths, path: str
) -> None:
    assert selected(components, ignored_paths, path) == {
        "backroom",
        "dittobench_api",
        "dittobench_coding_datagen",
        "dittobench_coding_starter_kit",
        "platform",
        "platform_api",
        "validator",
        "validator_stack",
    }
    assert root_verification(components, ignored_paths, path) == "full"


def test_platform_change_does_not_release_validator_stack(
    components, ignored_paths
) -> None:
    assert selected(
        components, ignored_paths, "apps/platform/ditto/api_server/factory.py"
    ) == {"platform_api", "platform", "backroom"}


def test_platform_workflow_is_release_owned(components, ignored_paths) -> None:
    assert selected(
        components,
        ignored_paths,
        ".github/workflows/platform-ci.yml",
    ) == {"platform_api", "platform", "backroom"}


def test_platform_dashboard_change_does_not_redeploy_backroom(
    components, ignored_paths
) -> None:
    assert selected(
        components,
        ignored_paths,
        "apps/platform/dashboard/src/pages/Leaderboard.tsx",
    ) == {"platform_dashboard", "platform"}


@pytest.mark.parametrize(
    "path",
    [
        "apps/platform/faircopy.config.mjs",
        "apps/platform/package.json",
        "apps/platform/package-lock.json",
    ],
)
def test_platform_dashboard_tooling_does_not_select_api_or_backroom(
    components, ignored_paths, path
) -> None:
    assert selected(components, ignored_paths, path) == {
        "platform_dashboard",
        "platform",
    }


def test_screener_change_does_not_release_validator_stack(
    components, ignored_paths
) -> None:
    assert selected(
        components, ignored_paths, "workers/screener/ditto_screener/worker.py"
    ) == {
        "screener",
        "screener_orchestrator",
    }


def test_orchestrator_change_is_isolated_from_validator_release(
    components, ignored_paths
) -> None:
    assert selected(
        components,
        ignored_paths,
        "services/screener-orchestrator/screener_capacity/controller.py",
    ) == {"screener_orchestrator"}


@pytest.mark.parametrize(
    "path",
    [
        "Dockerfile.screener-fleet-release",
        "scripts/build-screener-fleet-release.py",
        "scripts/screener-fleet-auto-update.sh",
    ],
)
def test_fleet_release_inputs_select_the_orchestrator(
    components, ignored_paths, path: str
) -> None:
    assert selected(components, ignored_paths, path) == {"screener_orchestrator"}


def test_screening_contract_change_propagates_to_every_consumer(
    components, ignored_paths
) -> None:
    assert selected(
        components,
        ignored_paths,
        "packages/ditto-screening-protocol/ditto_screening_protocol/models.py",
    ) == {
        "screening_protocol",
        "dittobench_api",
        "miner_cli",
        "validator",
        "validator_stack",
        "platform_api",
        "platform_dashboard",
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
    "paths",
    [
        ("ditto/miner_cli/commands/upload.py",),
        ("ditto/validator/worker.py",),
        ("packages/ditto-screening-protocol/ditto_screening_protocol/models.py",),
        ("scripts/sandbox-docker-entrypoint.sh",),
        ("pyproject.toml",),
        ("uv.lock",),
    ],
)
def test_root_runtime_changes_keep_full_verification(
    components, ignored_paths, paths: tuple[str, ...]
) -> None:
    assert root_verification(components, ignored_paths, *paths) == "full"


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/release.yml",
        "Dockerfile.pylon",
        "Dockerfile.stack-release",
        "docker-compose.yml",
        "release/components.toml",
        "scripts/build-stack-release.py",
        "scripts/release-plan.py",
    ],
)
def test_direct_stack_changes_use_focused_contract_verification(
    components, ignored_paths, path: str
) -> None:
    assert root_verification(components, ignored_paths, path) == "contract"


@pytest.mark.parametrize(
    "path",
    [
        "services/dittobench-api/cmd/dittobench-api/main.go",
        "apps/platform/ditto/api_server/factory.py",
        "apps/backroom/src/routes/index.tsx",
        "workers/screener/ditto_screener/worker.py",
        "miners/dittobench-starter-kit/src/baseline.rs",
        "miners/dittobench-coding-starter-kit/src/agent.rs",
        "docs/MONOREPO-RELEASES.md",
    ],
)
def test_isolated_components_skip_unrelated_root_verification(
    components, ignored_paths, path: str
) -> None:
    assert root_verification(components, ignored_paths, path) == "none"


def test_mixed_stack_and_root_changes_escalate_to_full_verification(
    components, ignored_paths
) -> None:
    assert (
        root_verification(
            components,
            ignored_paths,
            ".github/workflows/release.yml",
            "ditto/validator/worker.py",
        )
        == "full"
    )


def test_mixed_stack_and_isolated_component_uses_contract_verification(
    components, ignored_paths
) -> None:
    assert (
        root_verification(
            components,
            ignored_paths,
            ".github/workflows/release.yml",
            "services/dittobench-api/cmd/dittobench-api/main.go",
        )
        == "contract"
    )


def test_git_changed_paths_includes_deleted_runtime_files() -> None:
    with TemporaryDirectory() as directory:
        repository = Path(directory)
        subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repository,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Release Plan Test"],
            cwd=repository,
            check=True,
        )
        runtime = repository / "workers" / "screener" / "retired.py"
        runtime.parent.mkdir(parents=True)
        runtime.write_text("retired = True\n")
        subprocess.run(["git", "add", "."], cwd=repository, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "add runtime"], cwd=repository, check=True
        )
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        runtime.unlink()
        subprocess.run(["git", "add", "-u"], cwd=repository, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "delete runtime"], cwd=repository, check=True
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "release-plan.py"),
                "--config",
                str(ROOT / "release" / "components.toml"),
                "--base",
                base,
                "--head",
                head,
            ],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )

    plan = json.loads(completed.stdout)
    assert plan["components"]["screener"] is True
    assert plan["components"]["screener_orchestrator"] is True
    assert plan["root_verification"] == "none"


def test_github_output_includes_root_verification_mode(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    release_plan.write_github_output(
        output,
        {"validator_stack": True, "platform": False},
        "contract",
    )

    assert output.read_text().splitlines() == [
        "platform=false",
        "validator_stack=true",
        "any=true",
        "root_verification=contract",
    ]


def test_github_output_rejects_unknown_root_verification_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid root verification mode"):
        release_plan.write_github_output(tmp_path / "output", {}, "fast")


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
        (
            "ditto/api_models/confirmation_progress.py",
            {"validator", "validator_stack"},
        ),
        ("Dockerfile.pylon", {"validator_stack"}),
        (".dockerignore", {"validator_stack"}),
    ],
)
def test_live_runtime_inputs_are_mapped(
    components, ignored_paths, path: str, expected: set[str]
) -> None:
    assert selected(components, ignored_paths, path) == expected


def test_preview_package_changes_release_every_shipped_owner(
    components, ignored_paths
) -> None:
    assert selected(components, ignored_paths, "ditto/preview/engine.py") == {
        "miner_cli",
        "validator",
        "validator_stack",
    }


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
