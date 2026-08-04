from pathlib import Path

import yaml

WORKFLOW_PATH = Path(__file__).parents[2] / ".github/workflows/dittobench.yml"


def _step(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


def test_dittobench_workflow_uses_monorepo_contexts_without_repinning() -> None:
    text = WORKFLOW_PATH.read_text()
    workflow = yaml.safe_load(text)
    jobs = workflow["jobs"]

    assert "repin" not in text.lower()
    assert "services/dittobench-api/**" in text

    for job_name in ("docker-build", "provenance", "deploy"):
        build = next(
            step
            for step in jobs[job_name]["steps"]
            if step.get("uses", "").startswith("docker/build-push-action@")
        )
        assert build["with"]["context"] == "services/dittobench-api"
        assert build["with"]["file"] == "services/dittobench-api/Dockerfile"


def test_hosted_deploy_remains_push_only_and_commit_stamped() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text())
    deploy = workflow["jobs"]["deploy"]
    build = _step(deploy["steps"], "Build and push image")
    deploy_step = _step(deploy["steps"], "Deploy to Cloud Run")

    assert deploy["if"] == (
        "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    )
    assert "DITTOBENCH_SOURCE_SHA=${{ github.sha }}" in build["with"]["build-args"]
    assert "gcloud run deploy dittobench-api" in deploy_step["run"]


def test_every_dittobench_surface_triggers_ci() -> None:
    text = WORKFLOW_PATH.read_text()
    for path in (
        "services/dittobench-api/Dockerfile.egress-proxy",
        "services/dittobench-api/integrations/longmemeval/longmemeval_adapter.py",
        "services/dittobench-api/scripts/calibrate.sh",
        "services/dittobench-api/calibration/token-efficiency-v5/contract.json",
    ):
        # A single service-wide filter intentionally covers all current and
        # future API, research, integration, and image-build inputs.
        assert path.startswith("services/dittobench-api/")
        assert "services/dittobench-api/**" in text
