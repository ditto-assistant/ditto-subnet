from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def _triggers(workflow: dict) -> dict:
    return workflow.get("on", workflow[True])


def test_datagen_ci_syntax_checks_the_release_verifier() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/datagen-ci.yml").read_text())
    steps = workflow["jobs"]["build-test"]["steps"]
    syntax = next(
        step for step in steps if step.get("name") == "Validate release script syntax"
    )

    assert syntax["run"] == "bash -n scripts/verify-generate-service-release.sh"


def test_coding_datagen_ci_checks_committed_inference_vectors() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/coding-datagen-ci.yml").read_text()
    )
    step = next(
        item
        for item in workflow["jobs"]["verify"]["steps"]
        if item.get("name") == "Verify source, types, tests, and committed public pack"
    )
    check = (
        "uv run python ../../packages/dittobench-coding-contract/"
        "generate_inference_vectors.py --check"
    )
    assert check in step["run"].splitlines()
    assert (
        "uv run dittobench-coding-datagen validate-public-v2-pack "
        "/tmp/dittobench-coding-public-v2"
    ) in step["run"].splitlines()


def test_public_practice_has_no_hosted_dataset_upload_workflow() -> None:
    assert not (ROOT / ".github/workflows/publish-coding-practice.yml").exists()


def test_generate_release_verifier_uses_monorepo_paths_and_component_version() -> None:
    script = (
        ROOT / "research/dittobench-datagen/scripts/verify-generate-service-release.sh"
    ).read_text()

    assert 'module_dir="research/dittobench-datagen"' in script
    assert 'component_tag="v$declared_version"' in script
    assert '--file "$module_dir/cmd/generate-service/Dockerfile"' in script
    assert "printf 'component_tag=%s\\n'" in script
