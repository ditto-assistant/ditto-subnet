from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "preview-restore-snapshot.sh"


def test_restore_guard_rejects_non_preview_database(tmp_path: Path) -> None:
    marker = tmp_path / "pg_restore_called"
    fake = tmp_path / "pg_restore"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\n")
    fake.chmod(0o755)
    env = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PREVIEW_DATABASE_URL": "postgres://operator:secret@db.internal:5432/prod",
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path / "snapshot.dump")],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "refusing destructive restore" in result.stderr
    assert not marker.exists()
