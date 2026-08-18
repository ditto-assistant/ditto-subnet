from pathlib import Path

import yaml

WORKFLOW_PATH = Path(__file__).parents[2] / ".github/workflows/dittobench.yml"
RELEASE_WORKFLOW_PATH = Path(__file__).parents[2] / ".github/workflows/release.yml"


def _step(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


def test_dittobench_workflow_uses_monorepo_contexts_without_repinning() -> None:
    text = WORKFLOW_PATH.read_text()
    workflow = yaml.safe_load(text)
    jobs = workflow["jobs"]

    assert "repin" not in text.lower()
    assert "services/dittobench-api/**" in text
    assert "research/dittobench-datagen/**" in text
    assert "packages/ditto-screening-protocol/ditto_screening_protocol/data/**" in text

    for job_name in ("docker-build", "provenance"):
        build = next(
            step
            for step in jobs[job_name]["steps"]
            if step.get("uses", "").startswith("docker/build-push-action@")
        )
        assert build["with"]["context"] == "."
        assert build["with"]["file"] == "services/dittobench-api/Dockerfile"
        assert build["with"]["file"] == "services/dittobench-api/Dockerfile"


def test_hosted_deploy_is_release_only_and_commit_stamped() -> None:
    component = yaml.safe_load(WORKFLOW_PATH.read_text())
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    deploy = workflow["jobs"]["deploy-dittobench"]
    build = _step(deploy["steps"], "Publish the hosted runtime from the release commit")
    deploy_step = _step(deploy["steps"], "Deploy the immutable hosted image")

    assert "deploy" not in component["jobs"]
    assert "needs.plan.outputs.dittobench_api == 'true'" in deploy["if"]
    assert "DITTOBENCH_SOURCE_SHA=$SOURCE_SHA" in build["run"]
    assert "needs.release.outputs.commit_sha" in str(build["env"])
    assert "gcloud run deploy dittobench-api" in deploy_step["run"]
    assert '--image "$DITTOBENCH_HOSTED_REPOSITORY@$IMAGE_DIGEST"' in deploy_step["run"]
    assert ":sha-$SOURCE_SHA" not in deploy_step["run"]


def test_hosted_release_fails_closed_unless_current_contracts_are_advertised() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    deploy = workflow["jobs"]["deploy-dittobench"]
    verify = _step(
        deploy["steps"],
        "Verify the live practice endpoint reports its release identity",
    )

    assert "(.supported_bench_versions | sort == [8, 9, 10, 11, 12])" in verify["run"]


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
