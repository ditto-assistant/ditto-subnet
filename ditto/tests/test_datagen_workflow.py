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
        "uv run dittobench-coding-datagen build-public-release "
        "--pack practice/v1 --output /tmp/dittobench-coding-practice-release"
    ) in step["run"].splitlines()


def test_public_practice_publish_is_manual_public_and_content_addressed() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/publish-coding-practice.yml").read_text()
    )
    dispatch = _triggers(workflow)["workflow_dispatch"]["inputs"]
    publish = workflow["jobs"]["publish"]
    target_check = next(
        step
        for step in publish["steps"]
        if step.get("name") == "Require an existing public Hugging Face dataset target"
    )["run"]
    upload = next(
        step
        for step in publish["steps"]
        if step.get("name") == "Upload the content-addressed public artifact"
    )["run"]

    assert dispatch["confirmation"]["required"] is True
    assert "dataset_repository" not in dispatch
    assert publish["environment"] == "coding-practice-publish"
    assert "github.ref == 'refs/heads/main'" in publish["if"]
    assert "PUBLISH PUBLIC CODING PRACTICE PACK" in publish["if"]
    assert ".private == false" in target_check
    assert "releases/${{ steps.artifact.outputs.pack_id }}" in upload
    assert "HF_TOKEN" in publish["env"]
    assert (
        publish["env"]["HF_DATASET_REPOSITORY"]
        == "${{ vars.HF_CODING_PRACTICE_DATASET_REPO }}"
    )
    conflict = next(
        step
        for step in publish["steps"]
        if step.get("name") == "Refuse a conflicting immutable artifact"
    )["run"]
    assert "cmp --silent" in conflict
    assert "curl --location --silent" in conflict


def test_generate_release_verifier_uses_monorepo_paths_and_component_version() -> None:
    script = (
        ROOT / "research/dittobench-datagen/scripts/verify-generate-service-release.sh"
    ).read_text()

    assert 'module_dir="research/dittobench-datagen"' in script
    assert 'component_tag="v$declared_version"' in script
    assert '--file "$module_dir/cmd/generate-service/Dockerfile"' in script
    assert "printf 'component_tag=%s\\n'" in script
