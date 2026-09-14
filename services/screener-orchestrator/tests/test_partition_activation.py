"""Exercise the real drain helper with failed workload inventory commands."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("failure", ["virsh", "docker", "runuser", "container", "none"])
def test_partition_never_stops_rootless_on_unknown_workloads(tmp_path, failure):
    root = Path(__file__).resolve().parents[3]
    source = root / "infra/ansible/roles/screener_partition/files/activate-partition.sh"
    script = tmp_path / "activate.sh"
    script.write_text(
        source.read_text().replace(
            "/var/lib/ditto-screener-fleet/updater/lock", str(tmp_path / "lock")
        )
    )
    log = tmp_path / "commands"
    wrapper = """#!/bin/bash
name=${0##*/}
echo "$name $*" >> "$TEST_LOG"
if [[ "$name" == "$FAIL_COMMAND" && "$*" != *"docker info"* ]]; then exit 42; fi
if [[ "$FAIL_COMMAND" == container && "$*" == *"docker run"* ]]; then exit 43; fi
if [[ "$name" == systemctl && "$1" == show ]]; then
  if [[ "$2" == user@1005.service ]]; then
    echo /user.slice/user-1005.slice/user@1005.service
  else
    echo /dittoscreener.slice/service
  fi
fi
exit 0
"""
    for name in ("systemctl", "virsh", "docker", "runuser", "flock"):
        target = tmp_path / name
        target.write_text(wrapper)
        target.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{tmp_path}:{os.environ['PATH']}",
        TEST_LOG=str(log),
        FAIL_COMMAND=failure,
    )
    result = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, timeout=10
    )
    commands = log.read_text()
    if failure not in ("none", "container"):
        assert result.returncode != 0
        assert "systemctl stop user@1005.service" not in commands
        assert "systemctl start dittoscreener.slice" not in commands
    elif failure == "none":
        assert result.returncode == 0, result.stderr
        assert "systemctl disable ditto-screener-worker@2.service" in commands
    start = "systemctl start ditto-screener-fleet-agent.service "
    if failure in ("container", "runuser"):
        assert result.returncode != 0
        assert start not in commands
    else:
        assert start + "ditto-screener-worker@1.service\n" in commands
    assert (
        start + "ditto-screener-worker@1.service ditto-screener-worker@2"
        not in commands
    )
