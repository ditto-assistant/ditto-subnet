import tomllib
from pathlib import Path

import yaml

from ditto.validator.build_info import HEARTBEAT_PROTOCOL_VERSION

RELEASE_WORKFLOW_PATH = Path(__file__).parents[2] / ".github/workflows/release.yml"
CI_WORKFLOW_PATH = Path(__file__).parents[2] / ".github/workflows/ci.yml"
PYPROJECT_PATH = Path(__file__).parents[2] / "pyproject.toml"
ROOT_DOCKERFILE_PATH = Path(__file__).parents[2] / "Dockerfile"


def _step(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


def test_release_fanout_is_gated_by_the_component_plan() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]
    plan = jobs["plan"]
    resolver = _step(plan["steps"], "Resolve affected release components")

    assert (
        plan["outputs"]
        | {
            "miner_cli": "${{ steps.components.outputs.miner_cli }}",
            "validator_stack": "${{ steps.components.outputs.validator_stack }}",
        }
        == plan["outputs"]
    )
    assert {
        "miner_starter_kit",
        "dittobench_api",
        "dittobench_datagen",
        "platform",
        "backroom",
        "screener",
        "screener_orchestrator",
    } <= plan["outputs"].keys()
    assert "scripts/release-plan.py" in resolver["run"]
    assert plan["outputs"]["release_base"] == "${{ steps.release-base.outputs.sha }}"
    release_base = _step(plan["steps"], "Resolve the last published release")
    assert "0000000000000000000000000000000000000000" in release_base["run"]
    assert all(
        step.get("name") != "Require a new datagen component version"
        for step in plan["steps"]
    )
    assert jobs["release"]["needs"] == ["plan", "verify-source"]
    assert "always()" in jobs["release"]["if"]
    assert "needs.plan.result == 'success'" in jobs["release"]["if"]
    assert "needs.verify-source.result == 'success'" in jobs["release"]["if"]
    assert "needs.plan.outputs.miner_starter_kit == 'true'" in jobs["release"]["if"]
    assert jobs["admit-current"]["needs"] == "plan"
    assert (
        "needs.plan.outputs.miner_starter_kit == 'true'" in jobs["admit-current"]["if"]
    )
    assert jobs["verify-source"]["if"] == "always()"

    image_jobs = (
        "build-validator",
        "build-sandbox-docker",
        "build-dittobench",
        "assemble-stack",
        "smoke-validator-arm64",
        "promote-stack-release",
    )
    for job_name in image_jobs:
        job = jobs[job_name]
        assert "plan" in job["needs"]
        assert "needs.plan.outputs.validator_stack == 'true'" in job["if"]

    datagen = jobs["publish-datagen"]
    assert datagen["needs"] == ["plan", "release"]
    assert "needs.plan.outputs.dittobench_datagen == 'true'" in datagen["if"]
    publish = _step(datagen["steps"], "Publish immutable component and source tags")
    assert "$DATAGEN_REPOSITORY:$COMPONENT_TAG" in publish["run"]
    assert "$DATAGEN_REPOSITORY:sha-$SOURCE_SHA" in publish["run"]
    assert "gcloud artifacts docker tags add" in publish["run"]
    assert "reusing immutable datagen image" in publish["run"]
    assert "GCP_DATAGEN_RELEASE_SA" in str(datagen["steps"])
    smoke_auth = _step(datagen["steps"], "Mint the authenticated datagen smoke token")
    assert smoke_auth["id"] == "datagen-smoke-auth"
    assert smoke_auth["with"]["token_format"] == "id_token"
    assert smoke_auth["with"]["id_token_audience"] == "${{ env.DATAGEN_SERVICE_URL }}"
    deploy = _step(
        datagen["steps"], "Stage, verify, and deploy the immutable datagen image"
    )
    assert '--image="$image"' in deploy["run"]
    assert "--no-traffic" in deploy["run"]
    assert '--tag="$candidate_tag"' in deploy["run"]
    assert "for bench_version in 8 9" in deploy["run"]
    assert "bench_version=$bench_version" in deploy["run"]
    assert deploy["env"]["DATAGEN_ID_TOKEN"] == (
        "${{ steps.datagen-smoke-auth.outputs.id_token }}"
    )
    assert 'test "$service_url" = "$DATAGEN_SERVICE_URL"' in deploy["run"]
    assert "gcloud auth print-identity-token" not in deploy["run"]
    assert "Authorization: Bearer $DATAGEN_ID_TOKEN" in deploy["run"]
    assert "^x-bench-version: $bench_version$" in deploy["run"]
    assert '--remove-tags="$candidate_tag"' in deploy["run"]
    assert "--to-latest" in deploy["run"]


def test_release_auto_deploys_controller_and_builder_from_exact_release() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    deploy = workflow["jobs"]["deploy-screener-controller"]

    assert deploy["needs"] == [
        "plan",
        "release",
        "deploy_platform",
        "build-submission-builder",
    ]
    assert "needs.plan.outputs.screener_orchestrator == 'true'" in deploy["if"]
    assert "vars.SCREENER_CAPACITY_CONTROLLER_ENABLED == 'true'" in deploy["if"]
    assert deploy["uses"] == "./.github/workflows/screener-controller-deploy.yml"
    assert deploy["with"]["revision"] == "${{ needs.release.outputs.commit_sha }}"
    assert deploy["secrets"] == "inherit"
    assert deploy["permissions"] == {"contents": "read", "id-token": "write"}


def test_platform_and_backroom_deploy_from_one_release_plan() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]

    assert jobs["deploy_platform"]["with"]["revision"] == (
        "${{ needs.release.outputs.commit_sha }}"
    )
    assert jobs["deploy_platform"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }
    assert jobs["deploy-backroom"]["needs"] == ["plan", "release"]
    assert "needs.plan.outputs.backroom == 'true'" in jobs["deploy-backroom"]["if"]


def test_release_commits_the_refreshed_project_version_to_uv_lock() -> None:
    config = tomllib.loads(PYPROJECT_PATH.read_text())["tool"]["semantic_release"]
    build_command = config["build_command"]
    assert 'uv lock --upgrade-package "$PACKAGE_NAME"' in build_command
    assert "git add uv.lock" in build_command
    assert (
        "research/dittobench-datagen/internal/version/version.go:Version"
        in config["version_variables"]
    )

    # Verification runs before semantic release, so hosted deploys and image
    # publication cannot race ahead of the exact merged source gate.
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]
    verify_steps = jobs["verify-root"]["steps"]
    verify_checkout = _step(
        verify_steps, "Check out the exact merge commit before release"
    )
    assert verify_checkout["with"]["fetch-depth"] == 1
    platform_steps = jobs["verify-platform"]["steps"]
    node_setup = next(
        step
        for step in platform_steps
        if str(step.get("uses", "")).startswith("actions/setup-node@")
    )
    assert node_setup["with"]["node-version"] == 24
    verification = _step(verify_steps, "Gate the release on exact merge source")
    assert "uv sync --locked --group dev" in verification["run"].splitlines()
    assert workflow["jobs"]["release"]["needs"] == ["plan", "verify-source"]

    starter_verification = _step(
        jobs["verify-starter-kit"]["steps"],
        "Gate starter-kit release on exact merge source",
    )
    assert (
        "needs.plan.outputs.miner_starter_kit == 'true'"
        in (jobs["verify-starter-kit"]["if"])
    )
    assert "cargo build --locked --verbose" in starter_verification["run"]
    assert "cargo test --locked --verbose" in starter_verification["run"]

    model_relay_steps = jobs["verify-model-relay"]["steps"]
    model_relay_uv = _step(model_relay_steps, "Install uv")
    assert str(model_relay_uv["uses"]).startswith("astral-sh/setup-uv@")
    assert model_relay_uv["with"]["enable-cache"] is True

    datagen_verification = _step(
        jobs["verify-dittobench-datagen"]["steps"],
        "Gate DittoBench datagen release on exact merge source",
    )
    assert (
        "needs.plan.outputs.dittobench_datagen == 'true'"
        in (jobs["verify-dittobench-datagen"]["if"])
    )
    assert jobs["verify-dittobench-datagen"]["defaults"]["run"][
        "working-directory"
    ] == ("research/dittobench-datagen")
    assert "go test ./..." in datagen_verification["run"]

    component_gates = {
        "verify-platform": (
            "Gate Platform release on exact merge source",
            "needs.plan.outputs.platform == 'true'",
        ),
        "verify-backroom": (
            "Gate Backroom release on exact merge source",
            "needs.plan.outputs.backroom == 'true'",
        ),
        "verify-dittobench-api": (
            "Gate DittoBench API release on exact merge source",
            "needs.plan.outputs.dittobench_api == 'true'",
        ),
        "verify-screener": (
            "Gate screener release on exact merge source",
            "needs.plan.outputs.screener == 'true'",
        ),
        "verify-screener-orchestrator": (
            "Gate screener orchestrator release on exact merge source",
            "needs.plan.outputs.screener_orchestrator == 'true'",
        ),
    }
    for job_name, (step_name, condition) in component_gates.items():
        assert condition in jobs[job_name]["if"]
        _step(jobs[job_name]["steps"], step_name)

    aggregate = jobs["verify-source"]
    assert aggregate["if"] == "always()"
    assert {
        "verify-root",
        "verify-starter-kit",
        "verify-platform",
        "verify-model-relay",
        "verify-backroom",
        "verify-dittobench-api",
        "verify-dittobench-datagen",
        "verify-screener",
        "verify-screener-orchestrator",
    } < set(aggregate["needs"])
    gate = _step(aggregate["steps"], "Require every selected exact-source gate")
    assert 'test "$ROOT_RESULT" = success' in gate["run"]
    assert "require_selected" in gate["run"]


def test_superseded_candidate_skips_expensive_source_verification_early() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]
    admission = jobs["admit-current"]
    admission_step = _step(admission["steps"], "Admit only the current main candidate")

    assert admission["needs"] == "plan"
    assert admission["outputs"]["current"] == (
        "${{ steps.release-head.outputs.current }}"
    )
    assert "+refs/heads/main:refs/remotes/origin/main" in admission_step["run"]
    assert '[[ "$upstream_sha" != "${{ github.sha }}" ]]' in admission_step["run"]
    assert 'echo "current=false" >> "$GITHUB_OUTPUT"' in admission_step["run"]
    assert (
        "No source gate, version, tag, release, build, or deployment"
        in (admission_step["run"])
    )

    for job_name in (
        "verify-root",
        "verify-starter-kit",
        "verify-platform",
        "verify-model-relay",
        "verify-backroom",
        "verify-dittobench-api",
        "verify-dittobench-datagen",
        "verify-screener",
        "verify-screener-orchestrator",
    ):
        job = jobs[job_name]
        assert "admit-current" in job["needs"]
        assert "needs.admit-current.outputs.current == 'true'" in job["if"]


def test_superseded_verified_source_skips_release_mutations() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    release = workflow["jobs"]["release"]
    steps = release["steps"]
    release_head = _step(steps, "Classify a superseded release attempt")

    assert release["needs"] == ["plan", "verify-source"]
    assert release_head["id"] == "release-head"
    assert "+refs/heads/main:refs/remotes/origin/main" in release_head["run"]
    assert (
        "upstream_sha=$(git rev-parse refs/remotes/origin/main)" in release_head["run"]
    )
    assert '[[ "$upstream_sha" != "${{ github.sha }}" ]]' in release_head["run"]
    assert 'echo "current=false" >> "$GITHUB_OUTPUT"' in release_head["run"]
    assert 'echo "superseded=true" >> "$GITHUB_OUTPUT"' in release_head["run"]
    assert "No version, tag, release, or deployment" in release_head["run"]

    release_head_index = steps.index(release_head)
    semantic_release = _step(steps, "Version, tag, and create the GitHub release")
    assert release_head_index < steps.index(semantic_release)
    for name in (
        "Configure the release committer",
        "Bootstrap the existing v0.1.0 baseline",
        "Version, tag, and create the GitHub release",
    ):
        assert _step(steps, name)["if"] == (
            "steps.release-head.outputs.current == 'true'"
        )


def test_release_uses_the_root_projects_minimum_python() -> None:
    project = tomllib.loads(PYPROJECT_PATH.read_text())["project"]
    assert project["requires-python"] == ">=3.12,<3.14"

    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    verify_setup = _step(workflow["jobs"]["verify-root"]["steps"], "Set up Python 3.12")
    assemble_setup = _step(
        workflow["jobs"]["assemble-stack"]["steps"], "Set up Python 3.12"
    )
    assert verify_setup["run"] == "uv python install 3.12"
    assert assemble_setup["run"] == "uv python install 3.12"

    dockerfile = ROOT_DOCKERFILE_PATH.read_text().splitlines()
    assert dockerfile[0].startswith("FROM python:3.12-slim@sha256:")
    protocol_copy = dockerfile.index(
        "COPY packages/ditto-screening-protocol ./packages/ditto-screening-protocol"
    )
    frozen_sync = dockerfile.index("RUN uv sync --frozen --no-dev --extra telemetry")
    assert protocol_copy < frozen_sync


def test_screener_runner_fallback_requires_platform_authorization() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    job = workflow["jobs"]["build-screener"]
    steps = job["steps"]
    request = _step(steps, "Ask Platform for a Targon Kaniko build")
    fallback = _step(steps, "Build on the existing runner when Targon is unavailable")
    record = _step(steps, "Record immutable screener image identity")

    assert fallback["if"] == "steps.targon.outputs.mode == 'fallback'"
    assert "fallback_required)" in request["run"]
    assert "fallback_required|failed|canceled" not in request["run"]
    assert "no provider authorized a fallback" in request["run"]
    assert "timed out without a Platform-issued fallback" in request["run"]
    assert "reusing immutable fallback image" in fallback["run"]
    assert "Platform build poll unavailable; retrying" in request["run"]
    assert "gcloud artifacts docker images describe" in record["run"]
    assert '"$digest" != "$TARGON_DIGEST"' in record["run"]
    assert "--retry-all-errors" in record["run"]
    assert job["needs"] == [
        "plan",
        "release",
        "deploy_platform",
        "deploy-screener-controller",
    ]
    assert "needs.deploy-screener-controller.result == 'success'" in job["if"]
    assert "needs.deploy-screener-controller.result == 'skipped'" in job["if"]


def test_submission_builder_is_immutable_and_gates_controller_deploy() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]
    builder = jobs["build-submission-builder"]
    publish = _step(builder["steps"], "Publish the attempt-scoped Kaniko runner")
    controller = jobs["deploy-screener-controller"]

    assert builder["needs"] == ["plan", "release", "deploy_platform"]
    assert "needs.plan.outputs.screener_orchestrator == 'true'" in builder["if"]
    assert publish["env"]["SOURCE_SHA"] == "${{ needs.release.outputs.commit_sha }}"
    assert 'image="$SUBMISSION_BUILDER_REPOSITORY:sha-$SOURCE_SHA"' in publish["run"]
    assert 'docker push "$image"' in publish["run"]
    assert "GCP_SUBNET_BUILD_SA" in str(builder)
    assert "build-submission-builder" in controller["needs"]
    assert "needs.build-submission-builder.result == 'success'" in controller["if"]


def test_public_screener_dependency_needs_no_private_authentication() -> None:
    release_workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    release_steps = release_workflow["jobs"]["release"]["steps"]
    release = _step(release_steps, "Version, tag, and create the GitHub release")
    ci_workflow = yaml.safe_load(CI_WORKFLOW_PATH.read_text())
    install = _step(
        ci_workflow["jobs"]["lint-and-test"]["steps"], "Install dependencies"
    )

    assert install == {
        "name": "Install dependencies",
        "run": "uv sync --locked --group dev",
    }
    assert "env" not in release
    for workflow_path in (CI_WORKFLOW_PATH, RELEASE_WORKFLOW_PATH):
        text = workflow_path.read_text()
        assert "DITTO_SCREENER_PROTOCOL_READ_KEY" not in text
        assert "GIT_SSH_COMMAND" not in text
        assert "insteadOf" not in text


def test_compose_asset_download_is_pinned_and_retried() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW_PATH.read_text())
    install = _step(
        workflow["jobs"]["compose-config"]["steps"],
        "Install pinned Docker Compose ${{ matrix.compose-version }}",
    )

    assert "--retry 5 --retry-all-errors --retry-delay 2" in install["run"]
    assert "sha256sum --check --strict" in install["run"]


def test_retired_relay_bridge_uses_a_frozen_compatibility_source() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    relay_revision = workflow["env"]["MODEL_RELAY_COMPAT_REVISION"]
    assert len(relay_revision) == 40
    assert all(character in "0123456789abcdef" for character in relay_revision)

    build = workflow["jobs"]["build-dittobench"]
    source = _step(build["steps"], "Materialize the retired relay compatibility source")
    relay = _step(
        build["steps"], "Build and publish the retired relay compatibility index"
    )

    assert "SHA256SUMS" in source["run"]
    assert "relay_compat_revision" in source["run"]
    assert "dittobench-api.git" not in source["run"]
    assert relay["with"]["context"] == "${{ env.MODEL_RELAY_COMPAT_DIR }}"
    assert relay["with"]["file"] == ("${{ env.MODEL_RELAY_COMPAT_DIR }}/Dockerfile")
    assert (
        "org.opencontainers.image.revision="
        "${{ steps.dittobench-source.outputs.revision }}" in relay["with"]["labels"]
    )
    assert (
        "io.heyditto.validator.compat-source-revision="
        "${{ steps.relay-source.outputs.relay_compat_revision }}"
        in relay["with"]["labels"]
    )
    assert (
        "org.opencontainers.image.source="
        "https://github.com/ditto-assistant/dittobench-api" in relay["with"]["labels"]
    )
    assert "io.heyditto.validator.build-source=" in relay["with"]["labels"]
    assert "io.heyditto.validator.build-source-revision=" in relay["with"]["labels"]

    assembly = _step(
        workflow["jobs"]["assemble-stack"]["steps"],
        "Verify every first-party multi-platform index",
    )["run"]
    assert '["$MODEL_RELAY_REPOSITORY"]="$DITTOBENCH_REVISION"' in assembly
    assert (
        '["$MODEL_RELAY_REPOSITORY"]='
        '"https://github.com/ditto-assistant/dittobench-api"' in assembly
    )
    assert "io.heyditto.validator.build-source" in assembly
    assert "io.heyditto.validator.build-source-revision" in assembly
    assert "io.heyditto.validator.compat-source-revision" in assembly


def test_compat_channel_is_automatically_published_for_frozen_updaters() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]
    build = jobs["build-dittobench"]
    scorer = _step(build["steps"], "Build and publish the scorer index")
    relay = _step(
        build["steps"], "Build and publish the retired relay compatibility index"
    )
    promotion = _step(
        jobs["promote-stack-release"]["steps"],
        "Promote only the authenticated stack descriptor",
    )["run"]
    staging = _step(
        jobs["stage-stack-release"]["steps"],
        "Stage the authenticated descriptor for fleet prefetch",
    )["run"]

    # The first transition release must satisfy the frozen v0.47 updater,
    # while the relay retains its standalone compatibility identity. The
    # released updater accepts both scorer identities, allowing a subsequent
    # release to switch compat-2 back for v0.44 hosts.
    assert (
        "org.opencontainers.image.source="
        "https://github.com/ditto-assistant/ditto-subnet" in scorer["with"]["labels"]
    )
    assert (
        "org.opencontainers.image.source="
        "https://github.com/ditto-assistant/dittobench-api" in relay["with"]["labels"]
    )
    for image in (scorer, relay):
        labels = image["with"]["labels"]
        assert "org.opencontainers.image.version=" in labels
        assert "org.opencontainers.image.revision=" in labels
        assert "io.heyditto.validator.build-source=" in labels
        assert "io.heyditto.validator.build-source-revision=" in labels

    assert 'candidate="$STACK_REPOSITORY:candidate-compat-$COMPATIBILITY_EPOCH"' in (
        staging
    )
    assert 'test "$staged" = "$STACK_DIGEST"' in staging
    assert jobs["stage-stack-release"]["needs"] == [
        "plan",
        "release",
        "assemble-stack",
    ]
    assert "smoke-validator-arm64" in jobs["promote-stack-release"]["needs"]
    assert "stage-stack-release" in jobs["promote-stack-release"]["needs"]
    assert '--tag "$STACK_REPOSITORY:compat-$COMPATIBILITY_EPOCH"' in promotion
    assert 'test "$promoted" = "$STACK_DIGEST"' in promotion


def test_frozen_updater_descriptor_uses_direct_platform_manifests() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    assembly = workflow["jobs"]["assemble-stack"]
    runtime = _step(assembly["steps"], "Resolve frozen-updater runtime manifests")
    render = _step(assembly["steps"], "Render the architecture-bound stack bundles")[
        "run"
    ]

    assert runtime["id"] == "compat-runtime"
    assert "scripts/publish-compat-runtime-index.sh" not in runtime["run"]
    assert 'select(.platform.os == "linux"' in runtime["run"]
    assert "for architecture in amd64 arm64" in runtime["run"]
    assert (
        'resolve pylon_digest "$PYLON_REPOSITORY" "$PYLON_DIGEST" amd64'
        in runtime["run"]
    )

    outputs = {
        f"{component}_{architecture}_digest"
        for component in (
            "validator",
            "sandbox_docker",
            "dittobench_api",
            "model_relay",
        )
        for architecture in ("amd64", "arm64")
    } | {
        "pylon_digest",
    }
    for output in outputs:
        assert f"steps.compat-runtime.outputs.{output}" in render

    # Canonical build indexes remain the provenance-bearing source. Each signed
    # descriptor child binds direct runtime children selected from that index.
    assert "needs.build-validator.outputs.digest" not in render
    assert "build/stack-release-$architecture" in render
    dockerfile = (
        RELEASE_WORKFLOW_PATH.parents[2] / "Dockerfile.stack-release"
    ).read_text()
    assert "ARG TARGETARCH" in dockerfile
    assert "COPY build/stack-release-${TARGETARCH}/ /release/" in dockerfile
    verify = _step(assembly["steps"], "Verify every first-party multi-platform index")[
        "run"
    ]
    assert (
        '"$VALIDATOR_REPOSITORY"]="${{ needs.build-validator.outputs.digest }}"'
        in verify
    )


def test_validator_release_smokes_each_architecture_before_promotion() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]

    # The shared heartbeat constant is hoisted to workflow scope; the parallel
    # jobs read it from there instead of each declaring its own copy.
    assert workflow["env"]["HEARTBEAT_PROTOCOL"] == str(HEARTBEAT_PROTOCOL_VERSION)

    # Both jobs use ordinary GitHub-hosted x86 capacity. The arm64 lane installs
    # QEMU explicitly before it pulls and boots the exact arm64 child manifest.
    assert jobs["assemble-stack"]["runs-on"] == "ubuntu-24.04"
    assert jobs["smoke-validator-arm64"]["runs-on"] == "ubuntu-24.04"
    _step(
        jobs["smoke-validator-arm64"]["steps"],
        "Set up QEMU for the arm64 runtime smoke",
    )
    amd64_smoke = _step(
        jobs["assemble-stack"]["steps"],
        "Smoke-test the amd64 validator artifact by exact child digest",
    )
    arm64_smoke = _step(
        jobs["smoke-validator-arm64"]["steps"],
        "Smoke-test the arm64 validator artifact by exact child digest",
    )
    # Each smoke authenticates the arch it actually runs and asserts the
    # heartbeat-protocol label matches the release constant.
    assert "--platform linux/amd64" in amd64_smoke["run"]
    assert "--platform linux/arm64" in arm64_smoke["run"]
    for smoke in (amd64_smoke, arm64_smoke):
        assert '"$exact")" = "$HEARTBEAT_PROTOCOL"' in smoke["run"]

    # assemble-stack fans in from the re-verified source and every component
    # image, then builds and cosign-signs the immutable stack descriptor.
    assert set(jobs["assemble-stack"]["needs"]) == {
        "plan",
        "release",
        "verify-source",
        "build-validator",
        "build-sandbox-docker",
        "build-dittobench",
        "build-pylon",
    }
    sign_step = _step(
        jobs["assemble-stack"]["steps"],
        "Smoke-test and authenticate the exact stack descriptor",
    )
    assert 'cosign sign --yes "$exact"' in sign_step["run"]

    # The mutable discovery tag is promoted only after the descriptor is
    # assembled + signed (assemble-stack) AND both native validator smokes pass
    # (the amd64 smoke gates assemble-stack; the arm64 smoke gates directly).
    assert jobs["promote-stack-release"]["needs"] == [
        "plan",
        "release",
        "assemble-stack",
        "smoke-validator-arm64",
        "stage-stack-release",
    ]


def test_release_uses_the_bounded_larger_runner_only_on_cpu_bound_bottlenecks() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]
    larger_runner = {
        "group": "release-larger-runners",
        "labels": "ubuntu-24.04-release-8core",
    }

    assert jobs["verify-platform"]["runs-on"] == larger_runner
    assert jobs["build-dittobench"]["runs-on"] == larger_runner
    for job_name, job in jobs.items():
        if job_name not in {"verify-platform", "build-dittobench"}:
            assert job.get("runs-on") != larger_runner


def test_release_builds_pylon_from_the_reviewed_turbobt_fix() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    revision = workflow["env"]["PYLON_TURBOBT_REVISION"]
    assert len(revision) == 40
    assert all(character in "0123456789abcdef" for character in revision)

    build = workflow["jobs"]["build-pylon"]
    publish = _step(build["steps"], "Build and publish the patched Pylon image")
    assert publish["with"]["platforms"] == "linux/amd64"
    context = publish["with"]["build-contexts"]
    assert context.startswith(
        "turbobt=https://github.com/ditto-assistant/turbobt.git?"
        "ref=refs/heads/ditto/subscription-id-str&checksum="
    )
    assert context.endswith("${{ env.PYLON_TURBOBT_REVISION }}\n")

    assembly = workflow["jobs"]["assemble-stack"]
    verify = _step(assembly["steps"], "Verify the patched Pylon artifact")
    assert "isinstance(subscription_id_raw, str)" in verify["run"]
    runtime = _step(assembly["steps"], "Resolve frozen-updater runtime manifests")
    assert runtime["env"]["PYLON_DIGEST"] == "${{ needs.build-pylon.outputs.digest }}"
    assert (
        'resolve pylon_digest "$PYLON_REPOSITORY" "$PYLON_DIGEST" amd64'
        in runtime["run"]
    )
    render = _step(assembly["steps"], "Render the architecture-bound stack bundles")
    assert "steps.compat-runtime.outputs.pylon_digest" in render["run"]


def test_release_boots_exact_generated_runtime_dependencies_before_publish() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    steps = workflow["jobs"]["assemble-stack"]["steps"]
    render_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Render the architecture-bound stack bundles"
    )
    smoke_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Boot the exact release runtime dependencies"
    )
    publish_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Build and publish the immutable stack descriptor"
    )

    assert render_index < smoke_index < publish_index
    assert (
        "scripts/test-validator-stack-release-runtime.sh" in steps[smoke_index]["run"]
    )
    assert "build/stack-release-amd64/compose.yml" in steps[smoke_index]["run"]
    smoke_hotkey = steps[smoke_index]["env"]["VALIDATOR_HOTKEY"]
    assert smoke_hotkey == "5CZq6MdanxF3j8ACp8oVtiaphTeyrA7QFPU92ke2jEFzK1mp"
    assert smoke_hotkey != ("5Cg3DiRfrgzB1XzN7VuqQNchTgZ8PzPbphMKmVvHobWSL118")

    runtime_script = (
        RELEASE_WORKFLOW_PATH.parents[2]
        / "scripts"
        / "test-validator-stack-release-runtime.sh"
    ).read_text()
    assert "export VALIDATOR_STACK_DESCRIPTOR_REF=release-smoke" in runtime_script
    assert "export VALIDATOR_HOTKEY=release-smoke" in runtime_script


def test_release_scopes_each_github_actions_cache_to_one_image() -> None:
    """Concurrent release images use disjoint GitHub Actions cache scopes."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]

    scopes: dict[str, list[str]] = {}
    for job_name, job in jobs.items():
        for step in job.get("steps") or []:
            if "docker/build-push-action@" not in (step.get("uses") or ""):
                continue
            values = step.get("with") or {}
            cache_from = values.get("cache-from")
            cache_to = values.get("cache-to")
            assert cache_from and cache_from.startswith("type=gha,scope=")
            assert cache_to and cache_to.startswith("type=gha,mode=max,scope=")
            assert cache_from.removeprefix("type=gha,scope=") == cache_to.removeprefix(
                "type=gha,mode=max,scope="
            )
            scopes.setdefault(job_name, []).append(
                cache_from.removeprefix("type=gha,scope=")
            )

    assert scopes == {
        "build-validator": ["validator"],
        "build-sandbox-docker": ["sandbox-docker"],
        "build-pylon": ["pylon"],
        "build-dittobench": ["dittobench-api", "model-relay-compat"],
        "assemble-stack": ["stack-release"],
    }
    assert len({scope for job_scopes in scopes.values() for scope in job_scopes}) == 6
