#!/usr/bin/env python3
"""Run the brittle and robust red-team harnesses through the REAL scored pipeline.

Validates the audit end to end rather than in unit tests: does a surface-brittle
dispatcher actually get a low transform_robustness from the shipped scorer, and
does a genuinely robust local solver actually keep a high one?
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

API = "http://localhost:8000"
TMP = "/tmp/claude-1000"
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "redteam_harness.py")
MODE = sys.argv[1]
SEEDS = [int(s) for s in sys.argv[2].split(",")]
PORT = int(sys.argv[3])
OUT = f"{TMP}/redteam-{MODE}.json"


def post(path, payload):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def get(path):
    with urllib.request.urlopen(API + path, timeout=60) as r:
        return json.load(r)


results = []
for seed in SEEDS:
    # Fresh process per seed: the harness accumulates the haystack in memory, so
    # reusing it across seeds would let one persona's facts answer another's
    # questions.
    proc = subprocess.Popen(
        [sys.executable, SCRIPT, "--mode", MODE, "--port", str(PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    rec = {"seed": seed, "mode": MODE}
    try:
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://localhost:{PORT}/health", timeout=3):
                    break
            except Exception:
                time.sleep(0.5)
        started = time.time()
        sub = post(
            "/v1/submit",
            {"harness_url": f"http://localhost:{PORT}", "run_size": "full", "seed": seed},
        )
        rid = sub.get("run_id")
        rec["run_id"] = rid
        status = None
        while time.time() - started < 1800:
            time.sleep(10)
            d = get(f"/v1/runs/{rid}")
            status = d.get("status")
            if status in ("done", "completed", "failed", "error"):
                break
        d = get(f"/v1/runs/{rid}")
        rep = d.get("report") or {}
        det = rep.get("details") or {}
        rec.update(
            status=status,
            elapsed_sec=round(time.time() - started, 1),
            composite=rep.get("composite"),
            memory_mean=rep.get("memory_mean"),
            transform_robustness=det.get("transform_robustness"),
            audit_case_count=det.get("audit_case_count"),
            audit_pairs=det.get("audit_pairs"),
            metamorphic_consistency=det.get("metamorphic_consistency"),
        )
        audit = []
        for cs in rep.get("per_case") or []:
            tg = cs.get("twin_group") or ""
            if tg.startswith("auditxf-"):
                audit.append(
                    {"case_id": cs.get("case_id"), "twin_group": tg, "correct": cs.get("correct")}
                )
        rec["audit_cases"] = audit
    except Exception as exc:
        rec["error"] = repr(exc)
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=15)
        except Exception:
            pass
    results.append(rec)
    print(
        f"[{MODE} seed {seed}] tr={rec.get('transform_robustness')} "
        f"pairs={rec.get('audit_case_count')} mem={rec.get('memory_mean')} "
        f"{rec.get('elapsed_sec')}s {rec.get('error') or ''}",
        flush=True,
    )
    with open(OUT, "w") as fh:
        json.dump(results, fh, indent=2)

print(f"wrote {OUT}")
