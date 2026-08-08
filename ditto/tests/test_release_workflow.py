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
    assert "needs.plan.outputs.miner_starter_kit == 'true'" in jobs["release"]["if"]
    assert (
        "needs.plan.outputs.miner_starter_kit == 'true'" in jobs["verify-source"]["if"]
    )

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
    assert "bench_version=8" in deploy["run"]
    assert deploy["env"]["DATAGEN_ID_TOKEN"] == (
        "${{ steps.datagen-smoke-auth.outputs.id_token }}"
    )
    assert 'test "$service_url" = "$DATAGEN_SERVICE_URL"' in deploy["run"]
    assert "gcloud auth print-identity-token" not in deploy["run"]
    assert "Authorization: Bearer $DATAGEN_ID_TOKEN" in deploy["run"]
    assert "^x-bench-version: 8$" in deploy["run"]
    assert '--remove-tags="$candidate_tag"' in deploy["run"]
    assert "--to-latest" in deploy["run"]


def test_release_auto_deploys_controller_and_builder_from_exact_release() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    deploy = workflow["jobs"]["deploy-screener-controller"]

    assert deploy["needs"] == ["plan", "release", "deploy_platform"]
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
    verify_steps = workflow["jobs"]["verify-source"]["steps"]
    verify_checkout = _step(
        verify_steps, "Check out the exact merge commit before release"
    )
    assert verify_checkout["with"]["fetch-depth"] == 1
    node_setup = next(
        step
        for step in verify_steps
        if str(step.get("uses", "")).startswith("actions/setup-node@")
    )
    assert node_setup["with"]["node-version"] == 24
    verification = _step(verify_steps, "Gate the release on exact merge source")
    assert "uv sync --locked --group dev" in verification["run"].splitlines()
    assert workflow["jobs"]["release"]["needs"] == ["plan", "verify-source"]

    starter_verification = _step(
        verify_steps, "Gate starter-kit release on exact merge source"
    )
    assert starter_verification["if"] == (
        "needs.plan.outputs.miner_starter_kit == 'true'"
    )
    assert "cargo build --locked --verbose" in starter_verification["run"]
    assert "cargo test --locked --verbose" in starter_verification["run"]

    datagen_verification = _step(
        verify_steps, "Gate DittoBench datagen release on exact merge source"
    )
    assert datagen_verification["if"] == (
        "needs.plan.outputs.dittobench_datagen == 'true'"
    )
    assert datagen_verification["working-directory"] == ("research/dittobench-datagen")
    assert "go test ./..." in datagen_verification["run"]

    component_gates = {
        "Gate Platform release on exact merge source": (
            "needs.plan.outputs.platform == 'true'"
        ),
        "Gate Backroom release on exact merge source": (
            "needs.plan.outputs.backroom == 'true'"
        ),
        "Gate DittoBench API release on exact merge source": (
            "needs.plan.outputs.dittobench_api == 'true'"
        ),
        "Gate screener release on exact merge source": (
            "needs.plan.outputs.screener == 'true'"
        ),
        "Gate screener orchestrator release on exact merge source": (
            "needs.plan.outputs.screener_orchestrator == 'true'"
        ),
    }
    for name, condition in component_gates.items():
        assert _step(verify_steps, name)["if"] == condition


def test_release_uses_the_root_projects_minimum_python() -> None:
    project = tomllib.loads(PYPROJECT_PATH.read_text())["project"]
    assert project["requires-python"] == ">=3.12,<3.14"

    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    verify_setup = _step(
        workflow["jobs"]["verify-source"]["steps"], "Set up Python 3.12"
    )
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
    steps = workflow["jobs"]["build-screener"]["steps"]
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

    frozen_source = "https://github.com/ditto-assistant/dittobench-api"
    for image in (scorer, relay):
        labels = image["with"]["labels"]
        assert f"org.opencontainers.image.source={frozen_source}" in labels
        assert "org.opencontainers.image.version=" in labels
        assert "org.opencontainers.image.revision=" in labels
        assert "io.heyditto.validator.build-source=" in labels
        assert "io.heyditto.validator.build-source-revision=" in labels

    assert '--tag "$STACK_REPOSITORY:compat-$COMPATIBILITY_EPOCH"' in promotion
    assert 'test "$promoted" = "$STACK_DIGEST"' in promotion


def test_validator_release_smokes_each_architecture_natively_before_promotion() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]

    # The shared heartbeat constant is hoisted to workflow scope; the parallel
    # jobs read it from there instead of each declaring its own copy.
    assert workflow["env"]["HEARTBEAT_PROTOCOL"] == str(HEARTBEAT_PROTOCOL_VERSION)

    # The amd64 validator is smoke-tested natively inside assemble-stack (which
    # runs on the x86 fan-in runner); the arm64 validator is smoke-tested on a
    # native arm runner in parallel. Neither smoke relies on emulation.
    assert jobs["assemble-stack"]["runs-on"] == "blacksmith-4vcpu-ubuntu-2404"
    assert (
        jobs["smoke-validator-arm64"]["runs-on"] == "blacksmith-4vcpu-ubuntu-2404-arm"
    )
    amd64_smoke = _step(
        jobs["assemble-stack"]["steps"],
        "Smoke-test the amd64 validator artifact by exact child digest",
    )
    arm64_smoke = _step(
        jobs["smoke-validator-arm64"]["steps"],
        "Smoke-test the arm64 validator artifact by exact child digest",
    )
    # Each native smoke authenticates the arch it actually runs on and asserts
    # the heartbeat-protocol label matches the release constant.
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
    ]


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
    render = _step(assembly["steps"], "Render the digest-bound stack bundle")
    assert "needs.build-pylon.outputs.digest" in render["run"]


def test_release_boots_exact_generated_runtime_dependencies_before_publish() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    steps = workflow["jobs"]["assemble-stack"]["steps"]
    render_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Render the digest-bound stack bundle"
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
    assert "build/stack-release/compose.yml" in steps[smoke_index]["run"]


def test_release_scopes_each_docker_layer_cache_to_one_image() -> None:
    """Concurrent release builds must not share one layer cache.

    The image jobs fan out from the same ``needs``, and Blacksmith resolves
    concurrent committers to a cache key Last-Write-Wins. On a shared key only
    one of the parallel builds keeps its layers per release, so every build
    needs a key scoped to the image it actually builds.
    """
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]

    keys: dict[str, str] = {}
    for job_name, job in jobs.items():
        for step in job.get("steps") or []:
            if "useblacksmith/setup-docker-builder@" not in (step.get("uses") or ""):
                continue
            key = (step.get("with") or {}).get("cache-key")
            # An unkeyed builder falls back to the repository-wide cache that
            # every other image build also lands on.
            assert key, f"{job_name} sets up a builder without a cache-key"
            assert key.startswith("ditto-subnet/"), (
                f"{job_name} cache-key is not repository-scoped: {key}"
            )
            assert "${{" not in key, f"{job_name} cache-key is not a static string"
            keys[job_name] = key

    assert keys == {
        "build-validator": "ditto-subnet/Dockerfile",
        "build-sandbox-docker": "ditto-subnet/Dockerfile.sandbox-docker",
        "build-pylon": "ditto-subnet/Dockerfile.pylon",
        "build-dittobench": "ditto-subnet/services/dittobench-api",
        "assemble-stack": "ditto-subnet/Dockerfile.stack-release",
        # Neither of these builds an image; they only need buildx for
        # imagetools, so they stay off the caches the build jobs depend on.
        "smoke-validator-arm64": "ditto-subnet/release-manifest-tools",
        "promote-stack-release": "ditto-subnet/release-manifest-tools",
    }

    # No Dockerfile may be split across keys, and no key may collect Dockerfiles
    # that share no layers. Both cost cache hits on every release.
    for job_name, key in keys.items():
        built = {
            (step.get("with") or {}).get("file")
            for step in jobs[job_name]["steps"]
            if "build-push-action@" in (step.get("uses") or "")
        }
        if key == "ditto-subnet/release-manifest-tools":
            assert not built, f"{job_name} builds an image on the no-build key"
        elif len(built) == 1:
            assert built == {key.removeprefix("ditto-subnet/")}
        else:
            # An image set behind one builder: the scorer and the frozen relay
            # shim are built from one job and share a distroless runtime base.
            assert job_name == "build-dittobench"
            assert built == {
                "services/dittobench-api/Dockerfile",
                "${{ env.MODEL_RELAY_COMPAT_DIR }}/Dockerfile",
            }
            assert workflow["env"]["MODEL_RELAY_COMPAT_DIR"].startswith(
                key.removeprefix("ditto-subnet/")
            )
