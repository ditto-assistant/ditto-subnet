"""Release-path tests for the screener controller SSH deploy wrapper."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_controller_deploy_retries_os_login_key_propagation(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    fake_gcloud = tmp_path / "gcloud"
    fake_gcloud.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
cat >/dev/null
count=0
[ ! -f "$FAKE_GCLOUD_CALLS" ] || count="$(<"$FAKE_GCLOUD_CALLS")"
count=$((count + 1))
printf '%s' "$count" >"$FAKE_GCLOUD_CALLS"
[ "$count" -ge 3 ]
"""
    )
    fake_gcloud.chmod(0o755)
    script = Path(__file__).parents[1] / "scripts" / "deploy-via-ssh.sh"
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "GCP_PROJECT": "test-project",
        "FAKE_GCLOUD_CALLS": str(calls),
        "SCREENER_CONTROLLER_SSH_ATTEMPTS": "4",
        "SCREENER_CONTROLLER_SSH_RETRY_DELAY_SECONDS": "0",
    }

    result = subprocess.run(
        [script, "controller", "test-zone", "a" * 40],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text() == "3"
    assert "attempt 1/4 failed" in result.stderr
    assert "attempt 2/4 failed" in result.stderr
