"""Release-manifest checks for the future dedicated coding scorer artifact."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
RENDER = ROOT / "scripts/render-coding-executor-scorer-manifest.py"
DOCKERFILE = ROOT / "services/dittobench-api/Dockerfile"
RELEASE_WORKFLOW = ROOT / ".github/workflows/release.yml"
LOCKED_POLICY_FILE = (
    ROOT
    / "packages/dittobench-coding-contract/testdata"
    / "coding_inference_policy_locked_v1.json"
)
LOCKED_POLICY_SHA256 = (
    "b2f38d9f6b5484e9a056d74be4dc0250912f05c9e51512801b590dff934a41d6"
)
LOCKED_POLICY_FILE_SHA256 = (
    "6dd79225817b56ebf155f8344cd5faf752c8dd57802b21d6d2cbbae9cc2ff0b4"
)


def test_coding_executor_release_manifest_is_digest_bound(tmp_path: Path) -> None:
    output = tmp_path / "manifest.json"
    result = subprocess.run(
        [
            sys.executable,
            str(RENDER),
            "--image-reference",
            "registry.invalid/ditto/coding-scorer@sha256:" + "1" * 64,
            "--source-revision",
            "a" * 40,
            "--locked-policy-sha256",
            "2" * 64,
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text()) == {
        "image_digest": "sha256:" + "1" * 64,
        "image_reference": "registry.invalid/ditto/coding-scorer@sha256:" + "1" * 64,
        "locked_policy_sha256": "2" * 64,
        "platform": "linux/amd64",
        "schema": "dittobench-coding-executor-scorer-release-v1",
        "scorer_contract": "1",
        "source_revision": "a" * 40,
    }


def test_coding_executor_release_manifest_rejects_a_floating_image(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(RENDER),
            "--image-reference",
            "registry.invalid/ditto/coding-scorer:latest",
            "--source-revision",
            "a" * 40,
            "--locked-policy-sha256",
            "2" * 64,
            "--output",
            str(tmp_path / "manifest.json"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "repository@sha256" in result.stderr


def test_coding_executor_scorer_image_carries_its_runtime_contract() -> None:
    image = DOCKERFILE.read_text().split(
        "# ---- dedicated coding executor scorer artifact ----", 1
    )[1]
    workflow = RELEASE_WORKFLOW.read_text()
    assert "FROM docker:28.3.3-cli-alpine3.22 AS coding-executor-scorer" in image
    assert "coding_inference_policy_locked_v1.json" in image
    assert "chmod 0555 /opt/ditto /opt/ditto/coding" in image
    assert (
        "io.heyditto.dittobench.coding-executor-locked-policy-sha256="
        f'"{LOCKED_POLICY_SHA256}"' in image
    )
    assert "CMD []" in image
    assert "--entrypoint docker" in workflow
    assert LOCKED_POLICY_SHA256 in workflow
    assert LOCKED_POLICY_FILE_SHA256 in workflow
    assert hashlib.sha256(LOCKED_POLICY_FILE.read_bytes()).hexdigest() == (
        LOCKED_POLICY_FILE_SHA256
    )
