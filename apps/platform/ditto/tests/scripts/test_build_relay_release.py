from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

PLATFORM_ROOT = Path(__file__).parents[3]
MONOREPO_ROOT = PLATFORM_ROOT.parents[1]
SCRIPT = PLATFORM_ROOT / "scripts" / "build-relay-release.sh"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{source}")
    path.chmod(0o755)


def _run_builder(
    tmp_path: Path,
    *,
    local_export: bool = False,
    commit: str | None = None,
    platform_wheels: int = 1,
    protocol_wheels: int = 1,
    preexisting_artifact: bool = False,
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    artifact = tmp_path / "artifact"
    command_log = tmp_path / "commands.log"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=MONOREPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    if preexisting_artifact:
        artifact.mkdir()

    _write_executable(
        fake_bin / "uv",
        """
echo "uv $*" >> "$TEST_COMMAND_LOG"
case "$1" in
  build)
    out=""
    previous=""
    for arg in "$@"; do
      if [ "$previous" = --out-dir ]; then out="$arg"; fi
      previous="$arg"
    done
    mkdir -p "$out"
    if [[ "${@: -1}" == *ditto-screening-protocol ]]; then
      for ((i = 0; i < TEST_PROTOCOL_WHEELS; i++)); do
        touch "$out/ditto_screening_protocol-0.13.$i-py3-none-any.whl"
      done
    else
      for ((i = 0; i < TEST_PLATFORM_WHEELS; i++)); do
        version=$((i + 1))
        touch "$out/ditto_platform-0.0.$version-py3-none-any.whl"
      done
    fi
    ;;
  export)
    output=""
    previous=""
    for arg in "$@"; do
      if [ "$previous" = --output-file ]; then output="$arg"; fi
      previous="$arg"
    done
    if [ "${TEST_LOCAL_EXPORT:-0}" = 1 ]; then
      printf '%s%s\n' 'ditto-screening-protocol @ file:///packages/' \
        'ditto-screening-protocol' > "$output"
    else
      printf '%s\n' 'httpx==0.28.1 --hash=sha256:abc' > "$output"
    fi
    ;;
  venv)
    target="${@: -1}"
    mkdir -p "$target/bin"
    cat > "$target/bin/python" <<'PYTHON'
#!/usr/bin/env bash
exit 0
PYTHON
    chmod +x "$target/bin/python"
    ;;
  pip) ;;
esac
""",
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TEST_COMMAND_LOG": str(command_log),
        "TEST_LOCAL_EXPORT": "1" if local_export else "0",
        "TEST_PLATFORM_WHEELS": str(platform_wheels),
        "TEST_PROTOCOL_WHEELS": str(protocol_wheels),
    }
    result = subprocess.run(
        [str(SCRIPT), str(artifact), commit or head],
        cwd=MONOREPO_ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    commands = command_log.read_text() if command_log.exists() else ""
    return result, artifact, commands, head


def test_builder_packages_both_wheels_and_smokes_release_install(
    tmp_path: Path,
) -> None:
    result, artifact, commands, head = _run_builder(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (artifact / "source-commit").read_text().strip() == head
    assert len(list(artifact.glob("ditto_platform-*.whl"))) == 1
    assert len(list(artifact.glob("ditto_screening_protocol-*.whl"))) == 1
    requirements = (artifact / "requirements.lock").read_text()
    assert "file:" not in requirements
    assert "ditto-screening-protocol" not in requirements
    assert "--no-emit-package ditto-screening-protocol" in commands
    assert "pip install --python" in commands
    assert "--requirement" in commands
    assert commands.index("ditto_screening_protocol-0.13.0") < commands.index(
        "ditto_platform-0.0.1"
    )


def test_builder_fails_closed_on_local_path_export(tmp_path: Path) -> None:
    result, artifact, commands, _ = _run_builder(tmp_path, local_export=True)

    assert result.returncode != 0
    assert "local shared-package reference" in result.stderr
    assert not artifact.exists()
    assert "uv venv" not in commands


def test_builder_rejects_a_checkout_commit_mismatch_before_build(
    tmp_path: Path,
) -> None:
    result, artifact, commands, head = _run_builder(tmp_path, commit="0" * 40)

    assert result.returncode != 0
    assert "does not match checkout" in result.stderr
    assert not artifact.exists()
    assert commands == ""
    assert head


@pytest.mark.parametrize(
    ("platform_wheels", "protocol_wheels", "error"),
    [
        (0, 1, "exactly one platform wheel"),
        (2, 1, "exactly one platform wheel"),
        (1, 0, "exactly one screening-protocol wheel"),
        (1, 2, "exactly one screening-protocol wheel"),
    ],
)
def test_builder_rejects_missing_or_ambiguous_wheels(
    tmp_path: Path,
    platform_wheels: int,
    protocol_wheels: int,
    error: str,
) -> None:
    result, artifact, commands, _ = _run_builder(
        tmp_path,
        platform_wheels=platform_wheels,
        protocol_wheels=protocol_wheels,
    )

    assert result.returncode != 0
    assert error in result.stderr
    assert not artifact.exists()
    assert "uv venv" not in commands


def test_builder_never_overwrites_an_existing_artifact(tmp_path: Path) -> None:
    result, artifact, commands, _ = _run_builder(tmp_path, preexisting_artifact=True)

    assert result.returncode != 0
    assert "artifact path already exists" in result.stderr
    assert artifact.is_dir()
    assert commands == ""
