"""Release-manifest checks for the future dedicated coding scorer artifact."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
RENDER = ROOT / "scripts/render-coding-executor-scorer-manifest.py"


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
