from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

PLATFORM_ROOT = Path(__file__).parents[3]
MONOREPO_ROOT = PLATFORM_ROOT.parents[1]
SCRIPT = PLATFORM_ROOT / "scripts" / "build-relay-release.sh"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{source}")
    path.chmod(0o755)


def _write_module(
    tmp_path: Path,
    *,
    replace_directive: bool = False,
    with_go_sum: bool = True,
) -> Path:
    module = tmp_path / "module"
    (module / "cmd" / "model-relay").mkdir(parents=True)
    go_mod = "module github.com/ditto-assistant/model-relay\n\ngo 1.24\n"
    if replace_directive:
        go_mod += (
            "\nreplace github.com/ditto-assistant/other => ../../packages/other\n"
        )
    (module / "go.mod").write_text(go_mod)
    if with_go_sum:
        (module / "go.sum").write_text(
            "github.com/jackc/pgx/v5 v5.7.0 h1:abcdef\n"
        )
    (module / "cmd" / "model-relay" / "main.go").write_text("package main\n")
    return module


def _run_builder(
    tmp_path: Path,
    *,
    commit: str | None = None,
    replace_directive: bool = False,
    with_go_sum: bool = True,
    untidy: bool = False,
    no_binary: bool = False,
    preexisting_artifact: bool = False,
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    artifact = tmp_path / "artifact"
    command_log = tmp_path / "commands.log"
    module = _write_module(
        tmp_path,
        replace_directive=replace_directive,
        with_go_sum=with_go_sum,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=MONOREPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    if preexisting_artifact:
        artifact.mkdir()

    # The fake toolchain logs every invocation together with the cross-compile
    # environment, and `go build` materializes a runnable stand-in binary that
    # reports its ldflags so the script's --version self-check is exercised on
    # macOS and linux alike.
    _write_executable(
        fake_bin / "go",
        """
echo "go $* CGO_ENABLED=${CGO_ENABLED:-} GOOS=${GOOS:-} GOARCH=${GOARCH:-}" \
  >> "$TEST_COMMAND_LOG"
case "$1" in
  mod)
    if [ "$2" = tidy ] && [ "${TEST_UNTIDY:-0}" = 1 ]; then
      echo "go.sum is out of date" >&2
      exit 1
    fi
    ;;
  build)
    if [ "${TEST_NO_BINARY:-0}" = 1 ]; then
      exit 0
    fi
    out=""
    ldflags=""
    previous=""
    for arg in "$@"; do
      case "$previous" in
        -o) out="$arg" ;;
        -ldflags) ldflags="$arg" ;;
      esac
      previous="$arg"
    done
    cat > "$out" <<BINARY
#!/usr/bin/env bash
echo "model-relay \\$*" >> "$TEST_COMMAND_LOG"
echo "model-relay $ldflags"
BINARY
    chmod +x "$out"
    ;;
esac
""",
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TEST_COMMAND_LOG": str(command_log),
        "TEST_UNTIDY": "1" if untidy else "0",
        "TEST_NO_BINARY": "1" if no_binary else "0",
        "DITTO_RELAY_GO_MODULE_ROOT": str(module),
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


def _parse_sha256sums(artifact: Path) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in (artifact / "SHA256SUMS").read_text().splitlines():
        digest, _, name = line.partition("  ")
        sums[name.lstrip("*")] = digest
    return sums


def test_builder_produces_stamped_static_binary_artifact(tmp_path: Path) -> None:
    result, artifact, commands, head = _run_builder(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (artifact / "source-commit").read_text().strip() == head
    binary = artifact / "model-relay"
    assert binary.is_file()
    assert os.access(binary, os.X_OK)
    assert (artifact / "ecosystem.config.js").read_text() == (
        PLATFORM_ROOT / "scripts" / "ecosystem.config.js"
    ).read_text()
    assert (artifact / "deploy-relay-release.sh").read_text() == (
        PLATFORM_ROOT / "scripts" / "deploy-relay-release.sh"
    ).read_text()

    # The release build is a stamped, reproducible linux/amd64 cross-compile
    # with cgo off, from the module's cmd/model-relay package.
    release_builds = [
        line
        for line in commands.splitlines()
        if line.startswith("go build") and "GOOS=linux GOARCH=amd64" in line
    ]
    assert len(release_builds) == 1
    build = release_builds[0]
    assert "CGO_ENABLED=0" in build
    assert "-trimpath" in build
    assert f"-X main.buildCommit={head}" in build
    assert "./cmd/model-relay" in build
    assert "go mod tidy -diff" in commands
    assert "go mod verify" in commands

    # The smoke self-check ran against a runnable binary and saw the stamp.
    assert "model-relay --version" in commands

    sums = _parse_sha256sums(artifact)
    assert set(sums) == {
        "model-relay",
        "ecosystem.config.js",
        "deploy-relay-release.sh",
        "source-commit",
    }
    for name, digest in sums.items():
        assert digest == hashlib.sha256((artifact / name).read_bytes()).hexdigest()

    assert result.stdout.strip().splitlines()[-1] == f"relay-artifact={artifact}"


def test_builder_defaults_to_the_monorepo_go_module() -> None:
    source = SCRIPT.read_text()

    assert (
        'module_root="${DITTO_RELAY_GO_MODULE_ROOT:-'
        '$repo_root/services/model-relay}"' in source
    )


def test_builder_fails_closed_on_local_replace_directive(tmp_path: Path) -> None:
    result, artifact, commands, _ = _run_builder(tmp_path, replace_directive=True)

    assert result.returncode != 0
    assert "replace directive" in result.stderr
    assert not artifact.exists()
    assert commands == ""


def test_builder_requires_a_pinned_go_sum(tmp_path: Path) -> None:
    result, artifact, commands, _ = _run_builder(tmp_path, with_go_sum=False)

    assert result.returncode != 0
    assert "go.sum" in result.stderr
    assert not artifact.exists()
    assert commands == ""


def test_builder_rejects_an_untidy_module_before_building(tmp_path: Path) -> None:
    result, artifact, commands, _ = _run_builder(tmp_path, untidy=True)

    assert result.returncode != 0
    assert "not tidy" in result.stderr
    assert not artifact.exists()
    assert "go build" not in commands


def test_builder_rejects_a_checkout_commit_mismatch_before_build(
    tmp_path: Path,
) -> None:
    result, artifact, commands, head = _run_builder(tmp_path, commit="0" * 40)

    assert result.returncode != 0
    assert "does not match checkout" in result.stderr
    assert not artifact.exists()
    assert commands == ""
    assert head


def test_builder_rejects_a_build_that_produced_no_binary(tmp_path: Path) -> None:
    result, artifact, commands, _ = _run_builder(tmp_path, no_binary=True)

    assert result.returncode != 0
    assert "did not produce a model-relay binary" in result.stderr
    assert not artifact.exists()
    assert "model-relay --version" not in commands


def test_builder_never_overwrites_an_existing_artifact(tmp_path: Path) -> None:
    result, artifact, commands, _ = _run_builder(tmp_path, preexisting_artifact=True)

    assert result.returncode != 0
    assert "artifact path already exists" in result.stderr
    assert artifact.is_dir()
    assert commands == ""
