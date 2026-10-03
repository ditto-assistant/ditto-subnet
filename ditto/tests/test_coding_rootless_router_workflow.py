"""Static guards for the rootless router source-binding CI job."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/coding-rootless-router.yml"


def test_rootless_router_job_is_path_filtered_bounded_and_credential_free():
    text = WORKFLOW.read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    paths = workflow["on"]["pull_request"]["paths"]
    for path in (
        "services/dittobench-api/internal/rootlessnetns/**",
        "services/dittobench-api/cmd/dittobench-coding-router-listener/**",
        "services/dittobench-api/internal/codinghostedruntime/**",
        "services/dittobench-api/internal/codingcertifier/**",
        "services/dittobench-api/internal/codinggateway/**",
        "services/dittobench-api/internal/codingsource/**",
        "services/dittobench-api/internal/sandbox/**",
        ".github/workflows/coding-rootless-router.yml",
    ):
        assert path in paths
    # Every monorepo package the integration test imports directly triggers it.
    package = ROOT / "services/dittobench-api/internal/rootlessnetns"
    test = (package / "integration_linux_test.go").read_text()
    for imported in re.findall(
        r'"github\.com/ditto-assistant/dittobench-api/(internal/[a-z0-9_/]+)"', test
    ):
        assert f"services/dittobench-api/{imported}/**" in paths
    assert "services/dittobench-api/**" not in paths
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in text and "id-token" not in text
    assert "environment:" not in text
    (job,) = workflow["jobs"].values()
    assert job["runs-on"] == "ubuntu-24.04"
    assert 0 < int(job["timeout-minutes"]) <= 20
    checkout = job["steps"][0]
    assert checkout["with"]["persist-credentials"] == "false"


def test_rootless_router_job_pins_docker_and_runs_the_exact_integration_test():
    text = WORKFLOW.read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    env = workflow["env"]
    assert env["DOCKER_VERSION"] == "29.1.3"
    for key in ("DOCKER_SHA256", "DOCKER_ROOTLESS_EXTRAS_SHA256"):
        assert re.fullmatch(r"[0-9a-f]{64}", env[key])
    assert "sha256sum --check --strict" in text
    assert "https://download.docker.com/linux/static/stable/x86_64" in text
    # Only this disposable runner relaxes Ubuntu's user-namespace restriction.
    assert text.count("kernel.apparmor_restrict_unprivileged_userns=0") == 1
    assert "-tags rootless_router_integration" in text
    assert "-test.run '^TestRootlessRouterListenerPreservesContainerSource$'" in text
    for variable in (
        "DITTOBENCH_ROOTLESS_IT_DOCKER_SOCKET",
        "DITTOBENCH_ROOTLESS_IT_HELPER",
        "DITTOBENCH_ROOTLESS_IT_PROBE",
        "DITTOBENCH_ROOTLESS_IT_HOST_ADDRESS",
    ):
        assert variable in text
    package = ROOT / "services/dittobench-api/internal/rootlessnetns"
    test = (package / "integration_linux_test.go").read_text()
    assert test.startswith("//go:build rootless_router_integration\n")
    assert "t.Skip" not in test


def test_runtime_bundle_workflow_rebuilds_when_the_router_helper_changes():
    workflow = yaml.load(
        (ROOT / ".github/workflows/coding-hosted-runtime.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    paths = workflow["on"]["pull_request"]["paths"]
    for path in (
        "services/dittobench-api/cmd/dittobench-coding-router-listener/**",
        "services/dittobench-api/internal/rootlessnetns/**",
        "infra/containers/coding-hosted-runtime.Dockerfile",
    ):
        assert path in paths
