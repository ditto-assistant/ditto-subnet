import re
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

    identity_gate = "(.supported_bench_versions | sort == [8, 9, 10, 11, 12, 13])"
    assert identity_gate in verify["run"]


def _release_identity_gate_versions() -> list[int]:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    verify = _step(
        workflow["jobs"]["deploy-dittobench"]["steps"],
        "Verify the live practice endpoint reports its release identity",
    )
    gate = re.search(
        r"\.supported_bench_versions \| sort == \[([\d, ]+)\]", verify["run"]
    )
    assert gate is not None, "release.yml identity gate not found"
    return [int(v) for v in gate.group(1).split(",")]


def test_validator_executable_set_tracks_the_scorer_advertisement() -> None:
    """The validator must advertise exactly what the scorer advertises.

    ``ditto.validator.dittobench.SUPPORTED_BENCH_VERSIONS`` is intersected with
    the scorer's ``supported_bench_versions`` before the signed heartbeat, so a
    version the scorer offers but this tuple omits is dropped and Platform
    counts ZERO capable validators for it -- the exact v11 outage. The scorer's
    advertised set is pinned by the release deploy identity gate (which fails
    the deploy closed on any other set), so the two must be the same list.

    This is deliberately RED while the validator lags: the scorer half of a
    bench bump (#1519) cannot merge green until the wiring sweep that moves
    ``SUPPORTED_BENCH_VERSIONS`` (and the ``V9EvidenceBenchVersion`` Literal,
    starter-kit ``MAX_SUPPORTED_BENCH_VERSION``, local-rehearsal
    ``MAX_BENCH_VERSION`` and the regenerated contract goldens) has landed.
    """
    from ditto.validator.dittobench import SUPPORTED_BENCH_VERSIONS

    gate = _release_identity_gate_versions()
    assert sorted(SUPPORTED_BENCH_VERSIONS) == gate, (
        f"validator SUPPORTED_BENCH_VERSIONS={sorted(SUPPORTED_BENCH_VERSIONS)} "
        f"lags the scorer's advertised set {gate}: land the #1519 wiring sweep "
        "(ditto/validator/dittobench.py, ditto-screening-protocol bench_v9.py, "
        "starter-kit protocol.rs, local-rehearsal.py, contract goldens) first"
    )


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
