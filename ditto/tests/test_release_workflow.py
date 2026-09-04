import tomllib
from pathlib import Path

import yaml

from ditto.validator.build_info import HEARTBEAT_PROTOCOL_VERSION

RELEASE_WORKFLOW_PATH = Path(__file__).parents[2] / ".github/workflows/release.yml"
CI_WORKFLOW_PATH = Path(__file__).parents[2] / ".github/workflows/ci.yml"
CODING_STARTER_CI_PATH = (
    Path(__file__).parents[2] / ".github/workflows/coding-starter-kit-ci.yml"
)
ROOT_VERIFY_PATH = Path(__file__).parents[2] / ".github/workflows/root-verify.yml"
WORKFLOW_DIR = RELEASE_WORKFLOW_PATH.parent
PYPROJECT_PATH = Path(__file__).parents[2] / "pyproject.toml"
ROOT_DOCKERFILE_PATH = Path(__file__).parents[2] / "Dockerfile"
UNIFIED_STARTER_DOCKERFILE_PATH = (
    Path(__file__).parents[2] / "miners/dittobench-unified-starter-kit/Dockerfile"
)

RELEASE_OWNED_COMPONENT_WORKFLOWS = (
    "ci.yml",
    "backroom-ci.yml",
    "coding-datagen-ci.yml",
    "coding-starter-kit-ci.yml",
    "datagen-ci.yml",
    "dittobench.yml",
    "model-relay.yml",
    "platform-ci.yml",
    "platform-dashboard-ci.yml",
    "screener-ci.yml",
    "starter-kit-ci.yml",
)


def _step(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


def test_release_is_the_single_post_merge_component_orchestrator() -> None:
    release = yaml.load(RELEASE_WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)
    assert release["on"] == {"push": {"branches": ["main"]}}
    assert "actions/workflows" not in RELEASE_WORKFLOW_PATH.read_text()

    for workflow_name in RELEASE_OWNED_COMPONENT_WORKFLOWS:
        workflow = yaml.load(
            (WORKFLOW_DIR / workflow_name).read_text(), Loader=yaml.BaseLoader
        )
        triggers = workflow["on"]
        assert "pull_request" in triggers, workflow_name
        assert "workflow_dispatch" in triggers, workflow_name
        assert "push" not in triggers, workflow_name


def test_coding_starter_ci_tracks_the_public_contract_and_builds_the_image() -> None:
    workflow = yaml.load(CODING_STARTER_CI_PATH.read_text(), Loader=yaml.BaseLoader)
    paths = workflow["on"]["pull_request"]["paths"]
    assert "miners/dittobench-coding-starter-kit/**" in paths
    assert "miners/dittobench-unified-starter-kit/**" in paths
    assert "miners/dittobench-starter-kit/**" in paths
    assert "research/dittobench-coding-datagen/**" in paths
    assert "services/dittobench-api/internal/codingcontract/**" in paths
    assert "services/dittobench-api/internal/codingrelay/**" in paths
    assert "scripts/test-coding-starter-practice-e2e.sh" in paths
    verify = workflow["jobs"]["verify"]
    gate = _step(verify["steps"], "Verify format, lint, and tests")
    assert "cargo fmt --check" in gate["run"]
    assert (
        "cargo clippy --locked --all-targets --all-features -- -D warnings"
        in (gate["run"])
    )
    assert "cargo test --locked --all-targets --all-features --verbose" in gate["run"]
    cache = _step(verify["steps"], "Cache Cargo dependencies")
    assert "miners/dittobench-coding-starter-kit/target" in cache["with"]["path"]
    assert "miners/dittobench-unified-starter-kit/target" in cache["with"]["path"]
    image = _step(verify["steps"], "Build the shadow harness image")
    assert str(image["uses"]).startswith("docker/build-push-action@")
    assert image["with"]["context"] == "miners/dittobench-coding-starter-kit"
    assert "scope=coding-starter-kit" in image["with"]["cache-from"]
    assert "scope=coding-starter-kit" in image["with"]["cache-to"]
    unified = _step(verify["steps"], "Verify the unified normal and coding starter")
    assert unified["working-directory"] == "miners/dittobench-unified-starter-kit"
    assert "cargo run --locked -- submit" in unified["run"]
    assert "uv run ditto verify" in unified["run"]
    unified_image = _step(verify["steps"], "Build the unified starter image")
    assert str(unified_image["uses"]).startswith("docker/build-push-action@")
    assert unified_image["with"]["context"] == "miners"
    assert unified_image["with"]["file"] == (
        "miners/dittobench-unified-starter-kit/Dockerfile"
    )
    # The route probe below runs the tag from the local daemon.
    assert unified_image["with"]["load"] == "true"
    unified_probe = _step(verify["steps"], "Probe unified starter image routes")
    assert "test-image-health.sh" in unified_probe["run"]
    e2e = _step(verify["steps"], "Run the scripted Rust and Python practice E2E")
    assert "scripts/test-coding-starter-practice-e2e.sh" in e2e["run"]


def test_unified_starter_image_caches_dependencies_before_source() -> None:
    """Source-only edits must not invalidate the unified Rust dependency layer."""
    dockerfile = UNIFIED_STARTER_DOCKERFILE_PATH.read_text()

    manifest_copies = (
        (
            "COPY dittobench-starter-kit/Cargo.toml "
            "dittobench-starter-kit/Cargo.lock ./dittobench-starter-kit/",
            "COPY dittobench-starter-kit /app/dittobench-starter-kit",
        ),
        (
            "COPY dittobench-coding-starter-kit/Cargo.toml "
            "dittobench-coding-starter-kit/Cargo.lock "
            "./dittobench-coding-starter-kit/",
            "COPY dittobench-coding-starter-kit /app/dittobench-coding-starter-kit",
        ),
        (
            "COPY dittobench-unified-starter-kit/Cargo.toml "
            "dittobench-unified-starter-kit/Cargo.lock "
            "./dittobench-unified-starter-kit/",
            "COPY dittobench-unified-starter-kit /app/dittobench-unified-starter-kit",
        ),
    )
    warmup_build = "cargo build --locked --release --bin dittobench-unified-miner"

    assert "cargo clean --locked --release" in dockerfile
    assert "-p dittobench-starter-kit" in dockerfile
    assert "-p dittobench-coding-starter-kit" in dockerfile
    assert dockerfile.count(warmup_build) == 2
    for manifest_copy, source_copy in manifest_copies:
        assert manifest_copy in dockerfile
        assert dockerfile.index(manifest_copy) < dockerfile.index(warmup_build)
        assert dockerfile.index("cargo clean --locked --release") < dockerfile.index(
            source_copy
        )
        assert dockerfile.index(source_copy) < dockerfile.rindex(warmup_build)


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
        "dittobench_coding_starter_kit",
        "dittobench_api",
        "dittobench_coding_datagen",
        "dittobench_datagen",
        "platform",
        "backroom",
        "screener",
        "screener_orchestrator",
        "root_verification",
    } <= plan["outputs"].keys()
    assert plan["outputs"]["root_verification"] == (
        "${{ steps.components.outputs.root_verification }}"
    )
    assert "scripts/release-plan.py" in resolver["run"]
    assert plan["outputs"]["release_base"] == "${{ steps.release-base.outputs.sha }}"
    release_base = _step(plan["steps"], "Resolve the last published release")
    assert "0000000000000000000000000000000000000000" in release_base["run"]
    assert all(
        step.get("name") != "Require a new datagen component version"
        for step in plan["steps"]
    )
    assert jobs["release"]["needs"] == ["plan", "admit-current", "verify-source"]
    assert "always()" in jobs["release"]["if"]
    assert "needs.plan.result == 'success'" in jobs["release"]["if"]
    assert "needs.admit-current.outputs.current == 'true'" in jobs["release"]["if"]
    assert "needs.verify-source.result == 'success'" in jobs["release"]["if"]
    assert "needs.plan.outputs.miner_starter_kit == 'true'" in jobs["release"]["if"]
    assert (
        "needs.plan.outputs.dittobench_coding_starter_kit == 'true'"
        in jobs["release"]["if"]
    )
    assert (
        "needs.plan.outputs.dittobench_coding_datagen == 'true'"
        in jobs["release"]["if"]
    )
    assert jobs["admit-current"]["needs"] == "plan"
    assert (
        "needs.plan.outputs.miner_starter_kit == 'true'" in jobs["admit-current"]["if"]
    )
    assert (
        "needs.plan.outputs.dittobench_coding_starter_kit == 'true'"
        in jobs["admit-current"]["if"]
    )
    assert jobs["verify-source"]["if"] == "always()"
    assert "verify-dittobench-coding-datagen" in jobs["verify-source"]["needs"]
    assert "verify-dittobench-coding-starter-kit" in jobs["verify-source"]["needs"]
    coding_gate = jobs["verify-dittobench-coding-datagen"]
    assert "needs.plan.outputs.dittobench_coding_datagen == 'true'" in coding_gate["if"]
    assert coding_gate["defaults"]["run"]["working-directory"] == (
        "research/dittobench-coding-datagen"
    )
    coding_release_run = _step(
        coding_gate["steps"], "Gate coding datagen release on exact merge source"
    )["run"]
    assert (
        "../../packages/dittobench-coding-contract/"
        "generate_inference_vectors.py --check" in coding_release_run
    )
    coding_starter_gate = jobs["verify-dittobench-coding-starter-kit"]
    assert (
        "needs.plan.outputs.dittobench_coding_starter_kit == 'true'"
        in coding_starter_gate["if"]
    )
    assert coding_starter_gate["defaults"]["run"]["working-directory"] == (
        "miners/dittobench-coding-starter-kit"
    )
    cache_gate = _step(
        coding_starter_gate["steps"],
        "Cache coding starter-kit Cargo dependencies",
    )
    assert "miners/dittobench-coding-starter-kit/target" in cache_gate["with"]["path"]
    assert "miners/dittobench-unified-starter-kit/target" in cache_gate["with"]["path"]
    image_gate = _step(
        coding_starter_gate["steps"],
        "Build coding starter kit image from exact merge source",
    )
    assert str(image_gate["uses"]).startswith("docker/build-push-action@")
    assert image_gate["with"]["context"] == "miners/dittobench-coding-starter-kit"
    assert "scope=coding-starter-kit" in image_gate["with"]["cache-from"]
    assert "scope=coding-starter-kit" in image_gate["with"]["cache-to"]
    unified_gate = _step(
        coding_starter_gate["steps"],
        "Verify unified starter kit from exact merge source",
    )
    assert unified_gate["working-directory"] == "miners/dittobench-unified-starter-kit"
    assert "cargo run --locked -- submit" in unified_gate["run"]
    unified_image_gate = _step(
        coding_starter_gate["steps"],
        "Build unified starter kit image from exact merge source",
    )
    assert str(unified_image_gate["uses"]).startswith("docker/build-push-action@")
    assert unified_image_gate["with"]["context"] == "miners"
    assert unified_image_gate["with"]["file"] == (
        "miners/dittobench-unified-starter-kit/Dockerfile"
    )
    # The route probe below runs the tag from the local daemon.
    assert unified_image_gate["with"]["load"] is True
    unified_probe_gate = _step(
        coding_starter_gate["steps"],
        "Probe unified starter kit routes from exact merge source",
    )
    assert "test-image-health.sh" in unified_probe_gate["run"]
    e2e_gate = _step(
        coding_starter_gate["steps"],
        "Run scripted coding practice E2E from exact merge source",
    )
    assert "scripts/test-coding-starter-practice-e2e.sh" in e2e_gate["run"]
    source_gate = _step(
        jobs["verify-source"]["steps"],
        "Require every selected exact-source gate",
    )
    assert source_gate["env"]["CODING_STARTER_REQUIRED"] == (
        "${{ needs.plan.outputs.dittobench_coding_starter_kit }}"
    )
    assert source_gate["env"]["CODING_STARTER_RESULT"] == (
        "${{ needs.verify-dittobench-coding-starter-kit.result }}"
    )
    assert (
        'require_selected "$CODING_STARTER_REQUIRED" "$CODING_STARTER_RESULT" '
        "dittobench-coding-starter-kit" in source_gate["run"]
    )

    for job_name, job in jobs.items():
        if job_name in {
            "plan",
            "admit-current",
            "release",
            "verify-dittobench-coding-starter-kit",
            "verify-source",
        }:
            continue
        assert "dittobench_coding_starter_kit" not in str(job), job_name

    image_jobs = (
        "build-validator-amd64",
        "build-validator-arm64",
        "build-validator",
        "build-sandbox-docker",
        "build-dittobench-amd64",
        "build-dittobench-arm64",
        "build-model-relay-compat",
        "build-dittobench",
        "assemble-stack",
        "smoke-stack-runtime-amd64",
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


def test_post_release_fanout_evaluates_after_optional_verification_skips() -> None:
    jobs = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())["jobs"]
    expected_post_release_jobs = {
        "deploy_platform",
        "deploy-backroom",
        "build-submission-builder",
        "deploy-screener-controller",
        "build-screener",
        "assemble-screener-fleet-release",
        "build-validator-amd64",
        "build-validator-arm64",
        "build-validator",
        "build-sandbox-docker",
        "build-pylon",
        "prepare-dittobench",
        "build-coding-executor-scorer",
        "build-dittobench-amd64",
        "build-dittobench-arm64",
        "build-model-relay-compat",
        "build-dittobench",
        "publish-datagen",
        "deploy-dittobench",
        "assemble-stack",
        "smoke-stack-runtime-amd64",
        "smoke-validator-arm64",
        "stage-stack-release",
        "promote-stack-release",
    }
    post_release_jobs = {
        job_name
        for job_name, job in jobs.items()
        if "release"
        in (
            [job["needs"]]
            if isinstance(job.get("needs"), str)
            else job.get("needs", [])
        )
    }
    assert post_release_jobs == expected_post_release_jobs

    for job_name in post_release_jobs:
        condition = jobs[job_name]["if"]
        assert "always()" in condition
        assert "needs.plan.result == 'success'" in condition
        assert "needs.release.result == 'success'" in condition
        assert "needs.release.outputs.released == 'true'" in condition

    dependency_results = {
        "build-validator": (
            "build-validator-amd64",
            "build-validator-arm64",
        ),
        "build-dittobench-amd64": ("prepare-dittobench",),
        "build-dittobench-arm64": ("prepare-dittobench",),
        "build-model-relay-compat": ("prepare-dittobench",),
        "build-dittobench": (
            "prepare-dittobench",
            "build-dittobench-amd64",
            "build-dittobench-arm64",
        ),
        "deploy-dittobench": ("build-dittobench",),
        "assemble-stack": (
            "verify-source",
            "build-validator",
            "build-sandbox-docker",
            "build-dittobench",
            "build-model-relay-compat",
            "build-pylon",
        ),
        "smoke-stack-runtime-amd64": (
            "build-validator",
            "build-sandbox-docker",
            "build-dittobench",
            "build-model-relay-compat",
            "build-pylon",
        ),
        "smoke-validator-arm64": ("build-validator",),
        "stage-stack-release": ("assemble-stack",),
        "promote-stack-release": (
            "deploy_platform",
            "assemble-stack",
            "smoke-stack-runtime-amd64",
            "smoke-validator-arm64",
            "stage-stack-release",
        ),
    }
    for job_name, dependencies in dependency_results.items():
        condition = jobs[job_name]["if"]
        for dependency in dependencies:
            assert f"needs.{dependency}.result == 'success'" in condition


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
    root_run = jobs["verify-root-run"]
    assert root_run["uses"] == "./.github/workflows/root-verify.yml"
    assert root_run["with"]["ref"] == "${{ github.sha }}"
    assert root_run["with"]["mode"] == ("${{ needs.plan.outputs.root_verification }}")
    assert "needs.plan.outputs.root_verification == 'full'" in root_run["if"]
    assert "needs.plan.outputs.root_verification == 'contract'" in root_run["if"]
    platform_verify = jobs["verify-platform"]
    assert platform_verify["uses"] == "./.github/workflows/platform-verify.yml"
    assert platform_verify["with"]["ref"] == "${{ github.sha }}"
    assert platform_verify["with"]["backend"] == (
        "${{ needs.plan.outputs.platform_api == 'true' }}"
    )
    assert platform_verify["with"]["dashboard"] == (
        "${{ needs.plan.outputs.platform_dashboard == 'true' }}"
    )

    verifier = yaml.safe_load(ROOT_VERIFY_PATH.read_text())
    # yaml parses the `on:` trigger key as boolean True.
    assert verifier[True] == {
        "workflow_call": {
            "inputs": {
                "ref": {
                    "description": "Exact Git commit to verify",
                    "required": True,
                    "type": "string",
                },
                "mode": {
                    "description": "Verification depth (full or contract)",
                    "required": False,
                    "default": "full",
                    "type": "string",
                },
            }
        }
    }
    verifier_jobs = verifier["jobs"]
    root_static = verifier_jobs["static"]
    root_tests = verifier_jobs["tests"]
    root_contract = verifier_jobs["contract"]
    for root_job in (root_static, root_tests, root_contract):
        verify_checkout = root_job["steps"][0]
        assert verify_checkout["with"]["fetch-depth"] == 1
        assert verify_checkout["with"]["ref"] == "${{ inputs.ref }}"
        assert verify_checkout["with"]["persist-credentials"] is False
    for root_job in (root_static, root_tests):
        assert root_job["if"] == "inputs.mode == 'full'"
    assert root_contract["if"] == "inputs.mode == 'contract'"
    mode_gate = _step(
        verifier_jobs["validate"]["steps"], "Require a known verification mode"
    )
    assert "invalid root verification mode" in mode_gate["run"]
    verification = _step(
        root_static["steps"],
        "Root static and integration checks from the exact source",
    )
    assert "uv sync --locked --group dev" in verification["run"].splitlines()
    assert "uv run ruff format --check ." in verification["run"]
    assert "uv run ruff check ." in verification["run"]
    assert "uv run mypy ditto/" in verification["run"]
    assert "uv run pytest -m integration" in verification["run"]

    assert root_tests["strategy"] == {
        "fail-fast": False,
        "matrix": {"shard": [0, 1, 2]},
    }
    shard = _step(root_tests["steps"], "Root test shard from the exact source")
    assert shard["env"] == {"SHARD": "${{ matrix.shard }}", "SHARD_COUNT": 3}
    assert "uv run pytest --collect-only -q --color=no" in shard["run"]
    assert 'ditto/tests/*::*) test_nodeids+=("$test_nodeid")' in shard["run"]
    assert "index % SHARD_COUNT == SHARD" in shard["run"]
    assert 'uv run pytest "${shard_nodeids[@]}"' in shard["run"]

    contract = _step(
        root_contract["steps"],
        "Focused release contract from the exact source",
    )
    for test_path in (
        "ditto/tests/test_release_plan.py",
        "ditto/tests/test_release_workflow.py",
        "ditto/tests/test_workflow_security.py",
        "ditto/tests/test_compose_stack.py",
        "ditto/tests/test_validator_compose.py",
        "ditto/tests/test_validator_stack_auto_update.py",
    ):
        assert test_path in contract["run"]
    assert "uv run pytest -q -m integration" in contract["run"]

    root_aggregate = jobs["verify-root"]
    assert root_aggregate["if"] == "always()"
    assert set(root_aggregate["needs"]) == {
        "plan",
        "admit-current",
        "verify-root-run",
    }
    root_gate = _step(root_aggregate["steps"], "Require every root exact-source lane")
    assert root_gate["env"]["ROOT_VERIFICATION"] == (
        "${{ needs.plan.outputs.root_verification }}"
    )
    assert root_gate["env"]["RUN_RESULT"] == "${{ needs.verify-root-run.result }}"
    assert 'case "$ROOT_VERIFICATION" in' in root_gate["run"]
    assert 'test "$RUN_RESULT" = success' in root_gate["run"]
    assert 'test "$RUN_RESULT" = skipped' in root_gate["run"]
    assert "invalid root verification mode" in root_gate["run"]
    assert workflow["jobs"]["release"]["needs"] == [
        "plan",
        "admit-current",
        "verify-source",
    ]

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

    coding_starter_verification = _step(
        jobs["verify-dittobench-coding-starter-kit"]["steps"],
        "Gate coding starter kit release on exact merge source",
    )
    assert "cargo fmt --check" in coding_starter_verification["run"]
    assert (
        "cargo clippy --locked --all-targets --all-features -- -D warnings"
        in coding_starter_verification["run"]
    )
    assert (
        "cargo test --locked --all-targets --all-features --verbose"
        in coding_starter_verification["run"]
    )

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
        "verify-dittobench-coding-starter-kit": (
            "Gate coding starter kit release on exact merge source",
            "needs.plan.outputs.dittobench_coding_starter_kit == 'true'",
        ),
    }
    for job_name, (step_name, condition) in component_gates.items():
        assert condition in jobs[job_name]["if"]
        _step(jobs[job_name]["steps"], step_name)

    assert "needs.plan.outputs.platform == 'true'" in platform_verify["if"]

    aggregate = jobs["verify-source"]
    assert aggregate["if"] == "always()"
    assert {
        "verify-root",
        "verify-starter-kit",
        "verify-dittobench-coding-starter-kit",
        "verify-platform",
        "verify-model-relay",
        "verify-backroom",
        "verify-dittobench-api",
        "verify-dittobench-datagen",
        "verify-dittobench-coding-datagen",
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
        "verify-root-run",
        "verify-starter-kit",
        "verify-dittobench-coding-starter-kit",
        "verify-platform",
        "verify-model-relay",
        "verify-backroom",
        "verify-dittobench-api",
        "verify-dittobench-datagen",
        "verify-dittobench-coding-datagen",
        "verify-screener",
        "verify-screener-orchestrator",
    ):
        job = jobs[job_name]
        assert "admit-current" in job["needs"]
        assert "needs.admit-current.outputs.current == 'true'" in job["if"]

    root_aggregate = jobs["verify-root"]
    assert "admit-current" in root_aggregate["needs"]
    assert root_aggregate["if"] == "always()"


def test_superseded_verified_source_skips_release_mutations() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    release = workflow["jobs"]["release"]
    steps = release["steps"]
    release_head = _step(steps, "Classify a superseded release attempt")

    assert release["needs"] == ["plan", "admit-current", "verify-source"]
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
        "Install uv",
        "Install the locked release tool",
        "Version, tag, and create the GitHub release",
    ):
        assert _step(steps, name)["if"] == (
            "steps.release-head.outputs.current == 'true'"
        )


def test_release_uses_the_hash_locked_host_semantic_release_cli() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    steps = workflow["jobs"]["release"]["steps"]
    project = tomllib.loads(PYPROJECT_PATH.read_text())

    assert project["dependency-groups"]["release"] == [
        "python-semantic-release==10.6.1"
    ]
    assert not any(
        str(step.get("uses", "")).startswith(
            "python-semantic-release/python-semantic-release@"
        )
        for step in steps
    )

    release_head = _step(steps, "Classify a superseded release attempt")
    install_uv = _step(steps, "Install uv")
    install_release = _step(steps, "Install the locked release tool")
    semantic_release = _step(steps, "Version, tag, and create the GitHub release")
    current = "steps.release-head.outputs.current == 'true'"

    assert steps.index(release_head) < steps.index(install_uv)
    assert str(install_uv["uses"]).startswith("astral-sh/setup-uv@")
    assert install_uv["with"]["enable-cache"] is True
    assert install_uv["if"] == current
    assert install_release == {
        "name": "Install the locked release tool",
        "if": current,
        "run": "uv sync --locked --only-group release",
    }
    assert semantic_release["run"] == ("uv run --no-sync semantic-release -v version")
    assert semantic_release["env"] == {
        "GH_TOKEN": "${{ secrets.RELEASE_TOKEN }}",
        "GIT_COMMIT_AUTHOR": ("github-actions <actions@users.noreply.github.com>"),
        "PSR_DOCKER_GITHUB_ACTION": "true",
    }


def test_release_uses_the_root_projects_minimum_python() -> None:
    project = tomllib.loads(PYPROJECT_PATH.read_text())["project"]
    assert project["requires-python"] == ">=3.12,<3.14"

    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    verifier = yaml.safe_load(ROOT_VERIFY_PATH.read_text())
    verify_setups = [
        _step(verifier["jobs"][job_name]["steps"], "Set up Python 3.12")
        for job_name in ("static", "tests", "contract")
    ]
    assemble_setup = _step(
        workflow["jobs"]["assemble-stack"]["steps"], "Set up Python 3.12"
    )
    assert all(setup["run"] == "uv python install 3.12" for setup in verify_setups)
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
    assert "needs.release.outputs.released == 'true'" in builder["if"]
    assert "needs.plan.outputs.screener_orchestrator == 'true'" not in builder["if"]
    assert publish["env"]["SOURCE_SHA"] == "${{ needs.release.outputs.commit_sha }}"
    assert (
        publish["env"]["ORCHESTRATOR_REQUIRED"]
        == "${{ needs.plan.outputs.screener_orchestrator }}"
    )
    assert 'image="$SUBMISSION_BUILDER_REPOSITORY:sha-$SOURCE_SHA"' in publish["run"]
    assert 'docker push "$image"' in publish["run"]
    assert "gcloud artifacts docker tags add" in publish["run"]
    assert "GCP_SUBNET_BUILD_SA" in str(builder)
    assert "build-submission-builder" in controller["needs"]
    assert "needs.build-submission-builder.result == 'success'" in controller["if"]


def test_public_screener_dependency_needs_no_private_authentication() -> None:
    release_workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    release_steps = release_workflow["jobs"]["release"]["steps"]
    release = _step(release_steps, "Version, tag, and create the GitHub release")
    ci_workflow = yaml.safe_load(CI_WORKFLOW_PATH.read_text())
    verifier = yaml.safe_load(ROOT_VERIFY_PATH.read_text())

    # Pull-request CI runs the shared root verifier, whose dependency sync
    # must need no private authentication.
    assert ci_workflow["jobs"]["lint-and-test"]["uses"] == (
        "./.github/workflows/root-verify.yml"
    )
    assert ci_workflow["jobs"]["lint-and-test"]["with"] == {
        "ref": "${{ github.sha }}",
        "mode": "full",
    }
    static_gate = _step(
        verifier["jobs"]["static"]["steps"],
        "Root static and integration checks from the exact source",
    )
    assert "uv sync --locked --group dev" in static_gate["run"].splitlines()
    assert release["env"]["GH_TOKEN"] == "${{ secrets.RELEASE_TOKEN }}"
    for workflow_path in (CI_WORKFLOW_PATH, RELEASE_WORKFLOW_PATH, ROOT_VERIFY_PATH):
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

    prepare = workflow["jobs"]["prepare-dittobench"]
    build = workflow["jobs"]["build-model-relay-compat"]
    source = _step(
        prepare["steps"], "Materialize the retired relay compatibility source"
    )
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
        "${{ needs.prepare-dittobench.outputs.revision }}" in relay["with"]["labels"]
    )
    assert (
        "io.heyditto.validator.compat-source-revision="
        "${{ needs.prepare-dittobench.outputs.relay_compat_revision }}"
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
    build = jobs["build-model-relay-compat"]
    scorers = [
        _step(
            jobs[f"build-dittobench-{architecture}"]["steps"],
            f"Build and publish the native {architecture} scorer manifest",
        )
        for architecture in ("amd64", "arm64")
    ]
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
    for scorer in scorers:
        assert (
            "org.opencontainers.image.source="
            "https://github.com/ditto-assistant/ditto-subnet"
            in scorer["with"]["labels"]
        )
    assert (
        "org.opencontainers.image.source="
        "https://github.com/ditto-assistant/dittobench-api" in relay["with"]["labels"]
    )
    for image in (*scorers, relay):
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
    assert "smoke-stack-runtime-amd64" in jobs["promote-stack-release"]["needs"]
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
        "build-model-relay-compat",
        "build-pylon",
    }
    sign_step = _step(
        jobs["assemble-stack"]["steps"],
        "Smoke-test and authenticate the exact stack descriptor",
    )
    assert 'cosign sign --yes "$exact"' in sign_step["run"]

    # The mutable discovery tag is promoted only after the descriptor is
    # assembled + signed, the exact generated runtime boots, and both native
    # validator smokes pass.
    assert jobs["promote-stack-release"]["needs"] == [
        "plan",
        "release",
        "deploy_platform",
        "assemble-stack",
        "smoke-stack-runtime-amd64",
        "smoke-validator-arm64",
        "stage-stack-release",
    ]
    promotion_condition = jobs["promote-stack-release"]["if"]
    assert "needs.plan.outputs.platform == 'true'" in promotion_condition
    assert "needs.deploy_platform.result == 'success'" in promotion_condition
    assert "needs.deploy_platform.result == 'skipped'" in promotion_condition


def test_dittobench_prepare_does_not_repeat_exact_source_tests() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]
    verify = jobs["verify-dittobench-api"]
    prepare = jobs["prepare-dittobench"]

    exact_source_gate = _step(
        verify["steps"], "Gate DittoBench API release on exact merge source"
    )
    assert exact_source_gate["run"] == "go test ./..."
    _step(prepare["steps"], "Bind DittoBench to the monorepo release")
    materialize = _step(
        prepare["steps"], "Materialize the retired relay compatibility source"
    )
    assert "sha256sum --check SHA256SUMS" in materialize["run"]
    assert all(
        "actions/setup-go@" not in step.get("uses", "") for step in prepare["steps"]
    )
    assert all("go test ./..." not in step.get("run", "") for step in prepare["steps"])


def test_release_builds_dittobench_on_native_bounded_larger_runners() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]
    amd64_runner = {
        "group": "release-larger-runners",
        "labels": "ubuntu-24.04-release-8core",
    }
    arm64_runner = {
        "group": "release-larger-runners",
        "labels": "ubuntu-24.04-release-arm64-8core",
    }

    assert jobs["build-dittobench-amd64"]["runs-on"] == amd64_runner
    assert jobs["build-dittobench-arm64"]["runs-on"] == arm64_runner
    for job_name, job in jobs.items():
        if job_name != "build-dittobench-amd64":
            assert job.get("runs-on") != amd64_runner
        if job_name != "build-dittobench-arm64":
            assert job.get("runs-on") != arm64_runner

    for architecture in ("amd64", "arm64"):
        job = jobs[f"build-dittobench-{architecture}"]
        build = _step(
            job["steps"],
            f"Build and publish the native {architecture} scorer manifest",
        )
        assert build["with"]["platforms"] == f"linux/{architecture}"
        assert "push-by-digest=true" in build["with"]["outputs"]
        assert all(
            "setup-qemu-action@" not in (step.get("uses") or "")
            for step in job["steps"]
        )

    merge = _step(
        jobs["build-dittobench"]["steps"],
        "Assemble the scorer multi-platform index",
    )
    assert (
        merge["env"]["AMD64_DIGEST"]
        == "${{ needs.build-dittobench-amd64.outputs.digest }}"
    )
    assert (
        merge["env"]["ARM64_DIGEST"]
        == "${{ needs.build-dittobench-arm64.outputs.digest }}"
    )
    assert "docker buildx imagetools create" in merge["run"]

    compatibility = jobs["build-model-relay-compat"]
    assert compatibility["needs"] == ["plan", "release", "prepare-dittobench"]
    _step(
        compatibility["steps"],
        "Set up QEMU for the retired compatibility image",
    )
    fan_in_steps = jobs["build-dittobench"]["steps"]
    assert all(
        "setup-qemu-action@" not in (step.get("uses") or "") for step in fan_in_steps
    )
    assert all(
        step.get("name") != "Build and publish the retired relay compatibility index"
        for step in fan_in_steps
    )
    assert all(
        step.get("name") != "Check out the exact release commit"
        for step in fan_in_steps
    )


def test_release_builds_validator_on_native_standard_runners() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]

    assert jobs["build-validator-amd64"]["runs-on"] == "ubuntu-24.04"
    assert jobs["build-validator-arm64"]["runs-on"] == "ubuntu-24.04-arm"

    for architecture in ("amd64", "arm64"):
        job = jobs[f"build-validator-{architecture}"]
        build = _step(
            job["steps"],
            f"Build and publish the native {architecture} validator manifest",
        )
        assert build["with"]["platforms"] == f"linux/{architecture}"
        assert "push-by-digest=true" in build["with"]["outputs"]
        assert all(
            "setup-qemu-action@" not in (step.get("uses") or "")
            for step in job["steps"]
        )

    fan_in = jobs["build-validator"]
    assert set(fan_in["needs"]) == {
        "plan",
        "release",
        "build-validator-amd64",
        "build-validator-arm64",
    }
    merge = _step(fan_in["steps"], "Assemble the validator multi-platform index")
    assert (
        merge["env"]["AMD64_DIGEST"]
        == "${{ needs.build-validator-amd64.outputs.digest }}"
    )
    assert (
        merge["env"]["ARM64_DIGEST"]
        == "${{ needs.build-validator-arm64.outputs.digest }}"
    )
    assert "docker buildx imagetools create" in merge["run"]


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

    verify = _step(build["steps"], "Verify the patched Pylon artifact")
    assert verify["env"]["PYLON_DIGEST"] == "${{ steps.pylon.outputs.digest }}"
    assert "isinstance(subscription_id_raw, str)" in verify["run"]
    assert 'version("bittensor-wallet")=="4.1.1"' in verify["run"]
    assert '{"cryptoType":1,"ss58Address":address}' in verify["run"]
    assert "deserialize_keypair_from_keyfile_data(payload)" in verify["run"]
    assembly = workflow["jobs"]["assemble-stack"]
    assert all(
        step.get("name") != "Verify the patched Pylon artifact"
        for step in assembly["steps"]
    )
    runtime = _step(assembly["steps"], "Resolve frozen-updater runtime manifests")
    assert runtime["env"]["PYLON_DIGEST"] == "${{ needs.build-pylon.outputs.digest }}"
    assert (
        'resolve pylon_digest "$PYLON_REPOSITORY" "$PYLON_DIGEST" amd64'
        in runtime["run"]
    )
    render = _step(assembly["steps"], "Render the architecture-bound stack bundles")
    assert "steps.compat-runtime.outputs.pylon_digest" in render["run"]


def test_release_parallelizes_independent_artifact_authentication() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    verify = _step(
        workflow["jobs"]["assemble-stack"]["steps"],
        "Verify every first-party multi-platform index",
    )["run"]

    assert "verify_artifact()" in verify
    assert 'verify_artifact "$repository" &' in verify
    assert 'pids+=("$!")' in verify
    assert 'for pid in "${pids[@]}"' in verify
    assert 'wait "$pid"' in verify


def test_release_boots_exact_generated_runtime_dependencies_beside_publish() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]
    assembly_steps = jobs["assemble-stack"]["steps"]
    smoke = jobs["smoke-stack-runtime-amd64"]
    steps = smoke["steps"]
    assert all(
        step.get("name") != "Boot the exact release runtime dependencies"
        for step in assembly_steps
    )
    assert "assemble-stack" not in smoke["needs"]
    assert set(smoke["needs"]) == {
        "plan",
        "release",
        "build-validator",
        "build-sandbox-docker",
        "build-dittobench",
        "build-model-relay-compat",
        "build-pylon",
    }
    render_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Render the exact amd64 runtime bundle"
    )
    smoke_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Boot the exact release runtime dependencies"
    )
    assert render_index < smoke_index
    assert (
        "scripts/test-validator-stack-release-runtime.sh" in steps[smoke_index]["run"]
    )
    assert "build/stack-runtime-smoke-amd64/compose.yml" in steps[smoke_index]["run"]
    render = steps[render_index]["run"]
    assert "uv run --isolated --no-project --with pyyaml==6.0.3" in render
    assert "linux/amd64" in render
    assert "needs.build-validator.outputs.digest" in str(steps[render_index])
    smoke_hotkey = steps[smoke_index]["env"]["VALIDATOR_HOTKEY"]
    assert smoke_hotkey == "5CZq6MdanxF3j8ACp8oVtiaphTeyrA7QFPU92ke2jEFzK1mp"
    assert smoke_hotkey != ("5Cg3DiRfrgzB1XzN7VuqQNchTgZ8PzPbphMKmVvHobWSL118")
    assert "smoke-stack-runtime-amd64" in jobs["promote-stack-release"]["needs"]
    assert (
        "needs.smoke-stack-runtime-amd64.result == 'success'"
        in jobs["promote-stack-release"]["if"]
    )

    runtime_script = (
        RELEASE_WORKFLOW_PATH.parents[2]
        / "scripts"
        / "test-validator-stack-release-runtime.sh"
    ).read_text()
    assert "export VALIDATOR_STACK_DESCRIPTOR_REF=release-smoke" in runtime_script
    assert "export VALIDATOR_HOTKEY=release-smoke" in runtime_script
    assert "images=()" in runtime_script
    assert 'for image in "${images[@]}"' in runtime_script
    assert ") &" in runtime_script
    assert 'pids+=("$!")' in runtime_script
    assert 'wait "$pid"' in runtime_script
    compose_up = (
        'docker compose --project-name "$project" --file "$compose_file" '
        "\\\n  up --detach --wait"
    )
    assert runtime_script.index("pids=()") < runtime_script.index(compose_up)


def test_release_scopes_each_github_actions_cache_to_one_image() -> None:
    """Concurrent release images use disjoint GitHub Actions cache scopes."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]

    reader_scopes: dict[str, list[list[str]]] = {}
    writer_scopes: dict[str, list[str]] = {}
    for job_name, job in jobs.items():
        for step in job.get("steps") or []:
            if "docker/build-push-action@" not in (step.get("uses") or ""):
                continue
            values = step.get("with") or {}
            cache_from = values.get("cache-from")
            cache_to = values.get("cache-to")
            assert cache_from
            assert cache_to and cache_to.startswith("type=gha,mode=max,scope=")
            readers = [
                line.removeprefix("type=gha,scope=")
                for line in cache_from.splitlines()
                if line
            ]
            assert all(
                line.startswith("type=gha,scope=")
                for line in cache_from.splitlines()
                if line
            )
            # compression tunes the transport, never the scope identity.
            writer = cache_to.removeprefix("type=gha,mode=max,scope=").removesuffix(
                ",compression=zstd"
            )
            assert writer in readers
            reader_scopes.setdefault(job_name, []).append(readers)
            writer_scopes.setdefault(job_name, []).append(writer)

    assert writer_scopes == {
        "verify-dittobench-coding-starter-kit": [
            "coding-starter-kit",
            "unified-starter-kit",
        ],
        "build-validator-amd64": ["validator-amd64"],
        "build-validator-arm64": ["validator-arm64"],
        "build-sandbox-docker": ["sandbox-docker"],
        "build-pylon": ["pylon"],
        "build-dittobench-amd64": ["dittobench-api-amd64"],
        "build-coding-executor-scorer": ["coding-executor-scorer-amd64"],
        "build-dittobench-arm64": ["dittobench-api-arm64"],
        "build-model-relay-compat": ["model-relay-compat"],
        "assemble-stack": ["stack-release"],
        "assemble-screener-fleet-release": ["screener-fleet-release"],
    }
    assert reader_scopes["build-dittobench-amd64"] == [
        ["dittobench-api-amd64", "dittobench-api"]
    ]
    assert reader_scopes["build-dittobench-arm64"] == [
        ["dittobench-api-arm64", "dittobench-api"]
    ]
    assert reader_scopes["build-validator-amd64"] == [["validator-amd64", "validator"]]
    assert reader_scopes["build-validator-arm64"] == [["validator-arm64", "validator"]]
    assert (
        len({scope for job_scopes in writer_scopes.values() for scope in job_scopes})
        == 12
    )
