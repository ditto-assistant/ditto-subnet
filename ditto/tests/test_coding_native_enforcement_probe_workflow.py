"""Static guards for the rootless native enforcement probe-runner CI job."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/coding-native-enforcement-probe.yml"
PROBE = ROOT / "services/dittobench-api/internal/codingenforcement/probe"


def load() -> tuple[str, dict]:
    text = WORKFLOW.read_text()
    return text, yaml.load(text, Loader=yaml.BaseLoader)


API = ROOT / "services/dittobench-api"
MODULE = "github.com/ditto-assistant/dittobench-api/"
IMPORT = re.compile(
    r'"(github\.com/ditto-assistant/dittobench-(?:api|datagen)[a-z0-9_/.-]*)"'
)


def transitive_monorepo_paths() -> set[str]:
    """Monorepo trees the runner and its tagged tests import, transitively.

    Roots include their test files; dependencies contribute only production
    files, matching go list -deps -test for the runner packages.
    """

    roots = [
        "cmd/dittobench-coding-enforcement-probe",
        "internal/codingenforcement/probe",
    ]
    pending = [(root, True) for root in roots]
    seen: set[str] = set()
    paths: set[str] = set()
    while pending:
        package, include_tests = pending.pop()
        if package in seen:
            continue
        seen.add(package)
        for source in (API / package).glob("*.go"):
            if source.name.endswith("_test.go") and not include_tests:
                continue
            for imported in IMPORT.findall(source.read_text()):
                if imported.startswith("github.com/ditto-assistant/dittobench-datagen"):
                    paths.add("research/dittobench-datagen/**")
                elif imported.startswith(MODULE):
                    pending.append((imported[len(MODULE) :], False))
    for package in seen:
        paths.add(f"services/dittobench-api/{package}/**")
    return paths


def covered(path: str, filters: list[str]) -> bool:
    return any(
        path == item or (item.endswith("/**") and path.startswith(item[:-2]))
        for item in filters
    )


def test_probe_job_is_path_filtered_bounded_and_credential_free():
    text, workflow = load()
    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    paths = workflow["on"]["pull_request"]["paths"]
    assert "services/dittobench-api/**" not in paths
    required = transitive_monorepo_paths() | {
        "services/dittobench-api/go.mod",
        "services/dittobench-api/go.sum",
        "infra/scripts/coding-native-evidence.py",
        "ditto/tests/test_coding_native_probe_runner.py",
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/coding-native-enforcement-probe.yml",
    }
    assert "services/dittobench-api/internal/codinghostedworker/**" in required
    missing = sorted(path for path in required if not covered(path, paths))
    assert not missing, missing
    assert workflow["permissions"] == {"contents": "read"}
    for forbidden in ("secrets.", "id-token", "environment:", "self-hosted", "gcloud"):
        assert forbidden not in text
    (job,) = workflow["jobs"].values()
    assert job["runs-on"] == "ubuntu-24.04"
    assert 0 < int(job["timeout-minutes"]) <= 20
    assert job["steps"][0]["with"]["persist-credentials"] == "false"


def test_probe_job_pins_docker_delegates_cgroups_and_runs_the_exact_tests():
    text, workflow = load()
    env = workflow["env"]
    assert env["DOCKER_VERSION"] == "29.1.3"
    for key in ("DOCKER_SHA256", "DOCKER_ROOTLESS_EXTRAS_SHA256"):
        assert re.fullmatch(r"[0-9a-f]{64}", env[key])
    assert "sha256sum --check --strict" in text
    # Resource limits are only enforced with a delegated systemd cgroup driver.
    assert "Delegate=cpu cpuset io memory pids" in text
    assert "native.cgroupdriver=systemd" in text
    assert "io.heyditto.dittobench.isolated=true" in text
    assert "docker pull" not in text and "registry:" not in text
    assert "-tags native_probe_integration" in text
    test_name = "TestHostedGradingRequestedConfigMatchesTheApprovedProfile"
    assert f"-test.run '^{test_name}$'" in text
    assert "observe-requested-config" in text
    assert "testdata/ci-grading-profile.json" in text
    # Hosted grading refuses certification fixtures, so the image is not one.
    assert "coding-supervisor-fixture" not in text
    for variable in (
        "DITTOBENCH_NATIVE_PROBE_REPORT",
        "DITTOBENCH_REQUIRE_PROBE_RUNNER",
        "DITTOBENCH_REQUIRE_LIVE_PROBE_REPORT",
    ):
        assert variable in text
    assert "ditto/tests/test_coding_native_probe_runner.py" in text
    integration = (PROBE / "resource_integration_linux_test.go").read_text()
    assert integration.startswith("//go:build native_probe_integration\n")
    assert "t.Skip" not in integration
    assert "AllowCertificationImage" not in integration


def test_probe_runner_is_never_wired_to_a_host_workflow():
    workflows = ROOT / ".github/workflows"
    for path in workflows.glob("*.y*ml"):
        if path.name == WORKFLOW.name:
            continue
        text = path.read_text()
        assert "dittobench-coding-enforcement-probe" not in text or (
            path.name == "coding-native-release.yml"
        ), path.name
    operate = workflows / "coding-hosted-operate.yml"
    if operate.exists():
        assert "enforcement-probe" not in operate.read_text()
