from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Reference Test",
            "-c",
            "user.email=reference-test@example.invalid",
            "commit",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _bundle_digests(output: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in output.glob("reference_*_v2.bin")
    }


def test_generator_runs_without_platform_dependencies_and_covers_history(
    tmp_path: Path,
) -> None:
    starter = tmp_path / "starter"
    starter.mkdir()
    subprocess.run(["git", "-C", str(starter), "init", "-q"], check=True)
    (starter / "src.rs").write_text(
        'const PROMPT: &str = "Follow every public instruction and return the '
        'validated result without inventing any unavailable values.";\n'
        'fn first() {\n    let value = 1;\n    println!("{value}");\n}\n'
    )
    _commit(starter, "initial public scaffold")
    (starter / "src.rs").write_text(
        'const PROMPT: &str = "Follow every public instruction and return the '
        'validated result without inventing any unavailable values.";\n'
        'fn first() {\n    let value = 1;\n    println!("{value}");\n}\n\n'
        "fn later_public_helper() {\n"
        "    let value = 2;\n"
        '    println!("{value}");\n'
        "}\n"
    )
    _commit(starter, "extend public scaffold")

    output = tmp_path / "bundles"
    script = Path(__file__).parents[3] / "scripts" / "build_reference_fingerprints.py"
    command = [
        sys.executable,
        "-S",
        str(script),
        str(starter),
        "--revision",
        "HEAD",
        "--output",
        str(output),
    ]
    subprocess.run(command, check=True, cwd=tmp_path)

    manifest = json.loads((output / "reference_manifest_v2.json").read_text())
    assert manifest["revision"] == _git(starter, "rev-parse", "HEAD")
    assert len(manifest["commits"]) == 2
    assert manifest["unique_blobs"] == 2
    assert all(count > 0 for count in manifest["bundles"].values())

    first = _bundle_digests(output)
    subprocess.run(command, check=True, cwd=tmp_path)
    assert _bundle_digests(output) == first
