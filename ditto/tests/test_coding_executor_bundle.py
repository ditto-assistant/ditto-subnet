import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
RENDER = ROOT / "scripts/render-coding-executor-scorer-bundle.py"


def test_bundle_manifest_binds_the_release_manifest_and_archive(tmp_path: Path) -> None:
    release = tmp_path / "release.json"
    release.write_text(
        json.dumps(
            {
                "image_digest": "sha256:" + "1" * 64,
                "image_reference": "registry.invalid/scorer@sha256:" + "1" * 64,
                "locked_policy_sha256": "2" * 64,
                "platform": "linux/amd64",
                "schema": "dittobench-coding-executor-scorer-release-v1",
                "scorer_contract": "1",
                "source_revision": "a" * 40,
            }
        )
    )
    output = tmp_path / "bundle.json"
    result = subprocess.run(
        [
            sys.executable,
            str(RENDER),
            "--release-manifest",
            str(release),
            "--archive-sha256",
            "3" * 64,
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    bundle = json.loads(output.read_text())
    assert (
        bundle["release_manifest_sha256"]
        == hashlib.sha256(release.read_bytes()).hexdigest()
    )
    assert bundle["archive_sha256"] == "3" * 64
