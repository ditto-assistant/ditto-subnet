from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[3]
COMMIT = "a" * 40
RELEASE_SCRIPT = ROOT / "scripts" / "deploy-relay-release.sh"
ARTIFACT_FILES = (
    "model-relay",
    "ecosystem.config.js",
    "deploy-relay-release.sh",
    "source-commit",
)


def _write_executable(path: Path, source: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{source}")
    path.chmod(0o755)


def _run_release(
    tmp_path: Path,
    *,
    fail_relay_2: bool = False,
    artifact_commit: str = COMMIT,
    with_binary: bool = True,
    with_sha256sums: bool = True,
    corrupt_binary_after_sums: bool = False,
    strip_exec_bit: bool = False,
):
    artifact = tmp_path / "artifact"
    fake_bin = tmp_path / "bin"
    state = tmp_path / "state"
    old = tmp_path / "old"
    artifact.mkdir()
    fake_bin.mkdir()
    (old / "scripts").mkdir(parents=True)

    (artifact / "source-commit").write_text(f"{artifact_commit}\n")
    if with_binary:
        # A runnable stand-in for the statically linked Go binary: stays alive
        # as the canary until the deploy script SIGTERMs it.
        _write_executable(
            artifact / "model-relay",
            "trap 'exit 0' TERM INT\nwhile true; do sleep 1; done\n",
        )
    (artifact / "ecosystem.config.js").write_text("module.exports = {apps: []};\n")
    (artifact / "deploy-relay-release.sh").write_text("# shipped copy\n")
    if with_sha256sums:
        present = [name for name in ARTIFACT_FILES if (artifact / name).exists()]
        lines = [
            f"{hashlib.sha256((artifact / name).read_bytes()).hexdigest()}  {name}"
            for name in present
        ]
        (artifact / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    if corrupt_binary_after_sums:
        with (artifact / "model-relay").open("a") as handle:
            handle.write("# tampered after checksum generation\n")
    if strip_exec_bit:
        # actions/upload-artifact does not maintain file permissions: a CI
        # artifact round-trip hands the host a 0644 binary with intact bytes.
        (artifact / "model-relay").chmod(0o644)
    platform_env = tmp_path / "platform.env"
    platform_env.write_text(
        "PYLON_URL=http://127.0.0.1:1\nPERPLEXITY_API_KEY=direct-provider-test-key\n"
    )

    command_log = tmp_path / "commands.log"
    jlist = json.dumps(
        [
            {
                "name": f"ditto-api-relay-{index}",
                "pm2_env": {"pm_cwd": str(old), "status": "online"},
            }
            for index in (1, 2)
        ]
    )

    _write_executable(
        fake_bin / "curl",
        'printf \'{"commit":"%s"}\\n\' "$TEST_COMMIT"\n',
    )
    _write_executable(fake_bin / "flock", ":\n")
    _write_executable(
        fake_bin / "pm2",
        f"""
echo "pm2 $*" >> "$TEST_COMMAND_LOG"
echo "pm2 perplexity-bound=${{PERPLEXITY_API_KEY:+yes}}" >> "$TEST_COMMAND_LOG"
if [ "$1" = jlist ]; then
  printf '%s\\n' '{jlist}'
  exit 0
fi
if [ "${{FAIL_RELAY_2:-0}}" = 1 ] && [ "$1" = start ] \
    && [[ "$*" == *"ditto-api-relay-2"* ]]; then
  marker="$TEST_STATE_ROOT/relay-2-failed-once"
  if [ ! -e "$marker" ]; then
    touch "$marker"
    exit 1
  fi
fi
""",
    )

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TEST_COMMAND_LOG": str(command_log),
        "TEST_COMMIT": COMMIT,
        "TEST_STATE_ROOT": str(state),
        "DITTO_RELAY_STATE_ROOT": str(state),
        "DITTO_RELAY_PLATFORM_ENV": str(platform_env),
        "DITTO_RELAY_DEPLOY_ENV": str(tmp_path / "missing.deploy.env"),
        "DITTO_RELAY_START_TIMEOUT_SECONDS": "2",
        "FAIL_RELAY_2": "1" if fail_relay_2 else "0",
    }
    result = subprocess.run(
        [
            "bash",
            str(RELEASE_SCRIPT),
            str(artifact),
            COMMIT,
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )
    commands = command_log.read_text() if command_log.exists() else ""
    return result, commands, state, old


def test_release_installs_binary_once_and_rolls_slots_in_order(
    tmp_path: Path,
) -> None:
    result, commands, state, _ = _run_release(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (state / "ditto-api-relay-1.commit").read_text().strip() == COMMIT
    assert (state / "ditto-api-relay-2.commit").read_text().strip() == COMMIT

    # The immutable release dir holds the binary plus the pm2 config; there is
    # no venv and no host-side dependency resolution any more.
    release_dir = state / "releases" / COMMIT
    binary = release_dir / "model-relay"
    assert binary.is_file()
    assert os.access(binary, os.X_OK)
    assert (release_dir / "scripts" / "ecosystem.config.js").is_file()
    assert (release_dir / "logs").is_dir()

    assert commands.index("pm2 delete ditto-api-relay-1") < commands.index(
        "--only ditto-api-relay-1"
    )
    assert commands.index("--only ditto-api-relay-1") < commands.index(
        "pm2 delete ditto-api-relay-2"
    )
    assert "pm2 perplexity-bound=yes" in commands
    assert "relay-release-commit=" + COMMIT in result.stdout


def test_release_defaults_to_ansible_owned_monorepo_env() -> None:
    source = RELEASE_SCRIPT.read_text()

    assert (
        'platform_env="${DITTO_RELAY_PLATFORM_ENV:-'
        '/opt/ditto-subnet/apps/platform/.env}"' in source
    )
    assert (
        'deploy_env="${DITTO_RELAY_DEPLOY_ENV:-'
        '/opt/ditto-subnet/apps/platform/.env.deploy}"' in source
    )


def test_release_restores_failed_second_slot(tmp_path: Path) -> None:
    result, commands, state, old = _run_release(tmp_path, fail_relay_2=True)

    assert result.returncode != 0
    assert (state / "ditto-api-relay-1.commit").exists()
    assert not (state / "ditto-api-relay-2.commit").exists()
    restored = f"pm2 start {old}/scripts/ecosystem.config.js --only ditto-api-relay-2"
    assert restored in commands
    assert "restoring ditto-api-relay-2" in result.stderr


def test_release_normalizes_a_stripped_exec_bit(tmp_path: Path) -> None:
    # The CI artifact store (actions/upload-artifact) does not maintain file
    # permissions; a 0644 binary with intact bytes must still deploy — the
    # install -m 0755 normalizes the mode and SHA256SUMS proves integrity.
    result, _, state, _ = _run_release(tmp_path, strip_exec_bit=True)

    assert result.returncode == 0, result.stderr
    binary = state / "releases" / COMMIT / "model-relay"
    assert binary.is_file()
    assert os.access(binary, os.X_OK)


def test_release_rejects_a_missing_binary_before_install(tmp_path: Path) -> None:
    result, commands, state, _ = _run_release(tmp_path, with_binary=False)

    assert result.returncode != 0
    assert "model-relay binary" in result.stderr
    assert not (state / "releases").exists()
    assert "pm2" not in commands


def test_release_rejects_a_missing_checksum_manifest_before_install(
    tmp_path: Path,
) -> None:
    result, commands, state, _ = _run_release(tmp_path, with_sha256sums=False)

    assert result.returncode != 0
    assert "SHA256SUMS" in result.stderr
    assert not (state / "releases").exists()
    assert "pm2" not in commands


def test_release_rejects_a_tampered_binary_before_install(tmp_path: Path) -> None:
    result, commands, state, _ = _run_release(tmp_path, corrupt_binary_after_sums=True)

    assert result.returncode != 0
    assert "SHA256SUMS verification failed" in result.stderr
    assert not (state / "releases").exists()
    assert "pm2" not in commands


def test_release_rejects_artifact_commit_mismatch_before_install(
    tmp_path: Path,
) -> None:
    result, commands, state, _ = _run_release(tmp_path, artifact_commit="b" * 40)

    assert result.returncode != 0
    assert "artifact commit does not match" in result.stderr
    assert not (state / "releases").exists()
    assert "pm2" not in commands
