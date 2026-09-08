import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
RENDER = ROOT / "scripts/render-coding-executor-scorer-bundle.py"
EXPORT = ROOT / "scripts/export-coding-executor-scorer-bundle.sh"


def test_export_requires_the_exact_verified_release_attestation() -> None:
    source = EXPORT.read_text()
    assert "cosign verify-attestation" in source
    assert "--output json" in source
    assert "base64.b64decode" in source
    assert "canonical == release" in source
    assert (
        "release manifest is not the exact verified scorer attestation predicate"
        in source
    )


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
            "--image-id",
            "sha256:" + "4" * 64,
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
    assert bundle["image_id"] == "sha256:" + "4" * 64


def test_scorer_signer_and_exporter_share_a_valid_predicate_uri() -> None:
    import re
    from urllib.parse import urlsplit

    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    commands = "\n".join(
        step.get("run", "")
        for step in workflow["jobs"]["build-coding-executor-scorer"]["steps"]
    )
    types = re.findall(r"--type\s+(\S+)", commands)
    assert len(types) == 2
    assert types[0] == types[1]
    uri = urlsplit(types[0])
    assert uri.scheme == "https" and uri.netloc == "heyditto.ai" and uri.path
    exporter = EXPORT.read_text()
    assert re.findall(r"--type\s+(\S+)", exporter) == [types[0]]
    assert f'statement.get("predicateType") == "{types[0]}"' in exporter


def test_export_enforces_predicate_identity_before_pulling_image(
    tmp_path: Path,
) -> None:
    import base64
    import os

    predicate_type = (
        "https://heyditto.ai/attestations/coding-executor-scorer-release/v1"
    )
    release_data = {
        "image_digest": "sha256:" + "1" * 64,
        "image_reference": "registry.invalid/scorer@sha256:" + "1" * 64,
        "locked_policy_sha256": "2" * 64,
        "platform": "linux/amd64",
        "schema": "dittobench-coding-executor-scorer-release-v1",
        "scorer_contract": "1",
        "source_revision": "a" * 40,
    }
    release = tmp_path / "release.json"
    release.write_text(
        json.dumps(release_data, sort_keys=True, separators=(",", ":")) + "\n"
    )
    tools = tmp_path / "bin"
    tools.mkdir()
    cosign = tools / "cosign"
    # This is a transport stub, not a signature verifier: the test exercises
    # the real exporter after cosign verification succeeds or rejects a receipt.
    cosign.write_text(
        f"#!{sys.executable}\n"
        "import os,sys\n"
        "from pathlib import Path\n"
        f"assert sys.argv[sys.argv.index('--type')+1] == {predicate_type!r}\n"
        "if os.environ.get('REJECT_ATTESTATION'): sys.exit(1)\n"
        "print(Path(os.environ['ATTESTATION_FIXTURE']).read_text())\n"
    )
    cosign.chmod(0o755)
    docker = tools / "docker"
    marker = tmp_path / "docker-called"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import os,sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['DOCKER_MARKER']).touch()\n"
        "if sys.argv[1:3] == ['image','inspect']: print('sha256:'+'4'*64)\n"
        "if sys.argv[1:3] == ['image','save']:\n"
        " Path(sys.argv[sys.argv.index('--output')+1]).write_bytes(b'archive')\n"
    )
    docker.chmod(0o755)
    fixture = tmp_path / "attestation.json"
    cases = [
        ("matching", predicate_type, release_data, False, True),
        (
            "wrong_type",
            "io.heyditto.dittobench.coding-executor-scorer-release.v1",
            release_data,
            False,
            False,
        ),
        (
            "wrong_predicate",
            predicate_type,
            {**release_data, "source_revision": "b" * 40},
            False,
            False,
        ),
        ("unverified", predicate_type, release_data, True, False),
    ]
    for name, observed_type, predicate, rejected, accepted in cases:
        marker.unlink(missing_ok=True)
        output = tmp_path / name
        output.mkdir()
        statement = {"predicateType": observed_type, "predicate": predicate}
        fixture.write_text(
            json.dumps(
                [{"payload": base64.b64encode(json.dumps(statement).encode()).decode()}]
            )
        )
        env = {
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "ATTESTATION_FIXTURE": str(fixture),
            "DOCKER_MARKER": str(marker),
        }
        env.pop("REJECT_ATTESTATION", None)
        if rejected:
            env["REJECT_ATTESTATION"] = "1"
        result = subprocess.run(
            [
                "bash",
                str(EXPORT),
                "--release-manifest",
                str(release),
                "--output-dir",
                str(output),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert (result.returncode == 0) == accepted, (name, result.stderr)
        assert marker.exists() == accepted, name
        assert (output / "coding-executor-scorer.bundle.json").exists() == accepted, (
            name
        )
