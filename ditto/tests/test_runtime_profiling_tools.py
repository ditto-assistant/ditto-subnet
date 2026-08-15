from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
SKILL_SCRIPTS = (
    ROOT / ".agents" / "skills" / "ditto-subnet-runtime-profiling" / "scripts"
)
PROFILE_SUMMARY = SKILL_SCRIPTS / "summarize_python_profile.py"
CADDY_SUMMARY = SKILL_SCRIPTS / "summarize_caddy_access.py"


def _write_speedscope(
    path: Path,
    *,
    frames: list[dict[str, object]],
    samples: list[list[int]],
    weights: list[float],
    unit: str = "seconds",
) -> None:
    path.write_text(
        json.dumps(
            {
                "shared": {"frames": frames},
                "profiles": [
                    {
                        "type": "sampled",
                        "name": "test",
                        "unit": unit,
                        "startValue": 0,
                        "endValue": sum(weights),
                        "samples": samples,
                        "weights": weights,
                    }
                ],
            }
        )
    )


def test_python_profile_comparison_normalizes_and_ignores_line_shifts(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.json"
    current = tmp_path / "current.json"
    _write_speedscope(
        base,
        frames=[
            {"name": "route", "file": "public.py", "line": 100},
            {"name": "hot", "file": "query.py", "line": 10},
        ],
        samples=[[0, 1]],
        weights=[2.0],
    )
    _write_speedscope(
        current,
        frames=[
            {"name": "route", "file": "public.py", "line": 110},
            {"name": "hot", "file": "query.py", "line": 20},
            {"name": "cool", "file": "query.py", "line": 30},
        ],
        samples=[[0, 1], [0, 2]],
        weights=[1.0, 3.0],
    )

    completed = subprocess.run(
        [sys.executable, str(PROFILE_SUMMARY), str(current), "--base", str(base)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "comparison: normalized percentage points" in completed.stdout
    assert "frame_identity: function and file (source line ignored)" in completed.stdout
    assert "+75.00pp  cool (query.py)" in completed.stdout
    assert "-75.00pp  hot (query.py)" in completed.stdout
    assert "query.py:20" not in completed.stdout


def test_python_profile_single_summary_preserves_source_lines(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    _write_speedscope(
        profile,
        frames=[{"name": "hot", "file": "query.py", "line": 20}],
        samples=[[0]],
        weights=[1.0],
    )

    completed = subprocess.run(
        [sys.executable, str(PROFILE_SUMMARY), str(profile)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "hot (query.py:20)" in completed.stdout


def test_caddy_summary_reports_route_evidence_without_raw_uris(tmp_path: Path) -> None:
    access_log = tmp_path / "caddy.jsonl"
    records = [
        {
            "ts": 100,
            "request": {
                "method": "GET",
                "uri": "/api/v1/agent/a/pipeline?token=private-value",
            },
            "status": 200,
            "duration": 0.1,
        },
        {
            "ts": 101,
            "request": {
                "method": "GET",
                "uri": "/api/v1/agent/a/pipeline?token=private-value",
            },
            "status": 200,
            "duration": 0.2,
        },
        {
            "ts": 102,
            "request": {"method": "GET", "uri": "/api/v1/agent/b/pipeline"},
            "status": 200,
            "duration": 0.3,
        },
        {
            "ts": 103,
            "request": {"method": "GET", "uri": "/api/v1/agent/b/pipeline"},
            "status": 502,
            "duration": 0.001,
        },
        {
            "ts": 99,
            "request": {"method": "GET", "uri": "/health"},
            "status": 200,
            "duration": 0.01,
        },
    ]
    access_log.write_text("".join(f"{json.dumps(record)}\n" for record in records))

    completed = subprocess.run(
        [
            sys.executable,
            str(CADDY_SUMMARY),
            str(access_log),
            "--match",
            r"agent-pipeline=^/api/v1/agent/[^/]+/pipeline$",
            "--json",
            "--since",
            "1970-01-01T00:01:40Z",
            "--until",
            "104",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    route = result["routes"][0]

    assert result["parsed_records"] == 5
    assert result["window_records"] == 4
    assert result["window"] == {"since": 100.0, "until": 104.0}
    assert route == {
        "route": "agent-pipeline",
        "requests": 4,
        "success_2xx": 3,
        "status_counts": {"200": 3, "502": 1},
        "latency_2xx_ms": {"p50": 200.0, "p95": 300.0, "max": 300.0},
        "unique_uris": 2,
        "requests_per_uri": {"p50": 2.0, "p95": 2.0, "max": 2.0},
    }
    assert "private-value" not in completed.stdout
    assert "/api/v1/agent/" not in completed.stdout

    table = subprocess.run(
        [
            sys.executable,
            str(CADDY_SUMMARY),
            str(access_log),
            "--match",
            r"agent-pipeline=^/api/v1/agent/[^/]+/pipeline$",
            "--since",
            "100",
            "--until",
            "104",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "window_records: 4" in table.stdout
    assert "agent-pipeline" in table.stdout
    assert "200:3,502:1" in table.stdout
    assert "private-value" not in table.stdout


def test_caddy_summary_fails_closed_on_malformed_records(tmp_path: Path) -> None:
    access_log = tmp_path / "bad.jsonl"
    access_log.write_text('{"request": {"uri": "/health"}}\n')

    completed = subprocess.run(
        [sys.executable, str(CADDY_SUMMARY), str(access_log)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "line 1 has an invalid request method or URI" in completed.stderr
