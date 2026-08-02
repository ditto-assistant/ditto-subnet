from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]
COMMIT = "a" * 40


def _write_executable(path: Path, source: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{source}")
    path.chmod(0o755)


def _run_release(tmp_path: Path, *, fail_relay_2: bool = False):
    artifact = tmp_path / "artifact"
    fake_bin = tmp_path / "bin"
    state = tmp_path / "state"
    old = tmp_path / "old"
    artifact.mkdir()
    fake_bin.mkdir()
    (old / "scripts").mkdir(parents=True)

    (artifact / "source-commit").write_text(f"{COMMIT}\n")
    (artifact / "requirements.lock").write_text("# locked in CI\n")
    (artifact / "ditto_platform-0.0.1-py3-none-any.whl").write_text("wheel")
    (artifact / "ecosystem.config.js").write_text("module.exports = {apps: []};\n")
    platform_env = tmp_path / "platform.env"
    platform_env.write_text("PYLON_URL=http://127.0.0.1:1\n")

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
        fake_bin / "uv",
        f"""
echo "uv $*" >> "$TEST_COMMAND_LOG"
if [ "$1" = venv ]; then
  target="${{@: -1}}"
  mkdir -p "$target/bin"
  cat > "$target/bin/python" <<'PYTHON'
#!{sys.executable}
import signal, time
signal.signal(signal.SIGTERM, lambda *_: raise SystemExit(0))
while True:
    time.sleep(1)
PYTHON
  chmod +x "$target/bin/python"
fi
""",
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
            str(ROOT / "scripts" / "deploy-relay-release.sh"),
            str(artifact),
            COMMIT,
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )
    return result, command_log.read_text(), state, old


def test_release_builds_once_and_rolls_slots_in_order(tmp_path: Path) -> None:
    result, commands, state, _ = _run_release(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (state / "ditto-api-relay-1.commit").read_text().strip() == COMMIT
    assert (state / "ditto-api-relay-2.commit").read_text().strip() == COMMIT
    assert commands.count("uv venv") == 1
    assert commands.index("pm2 delete ditto-api-relay-1") < commands.index(
        "--only ditto-api-relay-1"
    )
    assert commands.index("--only ditto-api-relay-1") < commands.index(
        "pm2 delete ditto-api-relay-2"
    )
    assert "relay-release-commit=" + COMMIT in result.stdout


def test_release_restores_failed_second_slot(tmp_path: Path) -> None:
    result, commands, state, old = _run_release(tmp_path, fail_relay_2=True)

    assert result.returncode != 0
    assert (state / "ditto-api-relay-1.commit").exists()
    assert not (state / "ditto-api-relay-2.commit").exists()
    restored = f"pm2 start {old}/scripts/ecosystem.config.js --only ditto-api-relay-2"
    assert restored in commands
    assert "restoring ditto-api-relay-2" in result.stderr
