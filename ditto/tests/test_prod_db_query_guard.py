"""CASE plans remain read-only; transaction END must never reach SSH."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("query", "accepted"),
    [
        ("EXPLAIN (ANALYZE, BUFFERS) SELECT CASE WHEN true THEN 1 ELSE 0 END;", True),
        ("SELECT CASE WHEN true THEN CASE WHEN false THEN 1 END END;", True),
        ("SELECT 'CASE'; END;", False),
        ('SELECT "CASE"; END;', False),
        ("SELECT $$CASE$$; END;", False),
        ("SELECT $tag$CASE$tag$; END;", False),
        ("SELECT $$CASE; END$$;", True),
        ("SELECT CASE WHEN true THEN $tag$END$tag$ END;", True),
        ("SELECT 1; END;", False),
        ("SELECT CASE WHEN true THEN 1; END;", False),
        (
            "WITH changed AS (DELETE FROM agents RETURNING *) SELECT * FROM changed",
            False,
        ),
        ("SELECT 1; COMMIT;", False),
    ],
)
def test_case_and_transaction_boundaries(tmp_path, query, accepted):
    stub = tmp_path / "gcloud"
    captured = tmp_path / "wrapped.sql"
    stub.write_text('#!/bin/sh\ncat > "$GUARD_CAPTURE"\n')
    stub.chmod(0o700)
    script = (
        Path(__file__).resolve().parents[2]
        / ".agents/skills/gcloud-ditto-readonly/scripts/query_prod_db.sh"
    )
    result = subprocess.run(
        ["bash", str(script), query],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "GUARD_CAPTURE": str(captured),
        },
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is accepted, result.stderr
    assert captured.exists() is accepted
    if accepted:
        wrapped = captured.read_text()
        assert "BEGIN READ ONLY;" in wrapped
        assert wrapped.rstrip().endswith("ROLLBACK;")


@pytest.mark.parametrize("disable_jit", ["0", "1", "invalid"])
def test_jit_probe_is_connection_local_and_keeps_read_only(tmp_path, disable_jit):
    stub = tmp_path / "gcloud"
    captured = tmp_path / "ssh-args.txt"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$GUARD_CAPTURE"\ncat\n')
    stub.chmod(0o700)
    script = (
        Path(__file__).resolve().parents[2]
        / ".agents/skills/gcloud-ditto-readonly/scripts/query_prod_db.sh"
    )
    result = subprocess.run(
        ["bash", str(script), "SELECT 1"],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "GUARD_CAPTURE": str(captured),
            "DITTO_DB_DISABLE_JIT": disable_jit,
            "DITTO_DB_STATEMENT_TIMEOUT_MS": "30000",
        },
        capture_output=True,
        text=True,
    )
    if disable_jit == "invalid":
        assert result.returncode != 0 and not captured.exists()
        return
    assert result.returncode == 0
    args = captured.read_text()
    assert "default_transaction_read_only=on" in args
    assert "statement_timeout=30000" in args
    assert ("jit=off" in args) is (disable_jit == "1")
    assert "BEGIN READ ONLY;" in result.stdout and "ROLLBACK;" in result.stdout
