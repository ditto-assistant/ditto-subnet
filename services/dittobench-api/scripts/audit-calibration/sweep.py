#!/usr/bin/env python3
"""Honest-model transform-audit calibration sweep.

Drives the REAL scored pipeline (dittobench-api -> starter-kit harness ->
OpenRouter qwen/qwen3-32b) over a seed sweep and collects the
transform_robustness the platform would actually see.

Isolation matters and is the reason this starts a fresh harness per run: the
harness /seed endpoint is an idempotent upsert with no clear, so reusing one
process would stack several personas' haystacks into one store, depressing
retrieval and contaminating the very pairs being measured. A real scored run
faces a fresh container, so this does too.
"""

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request

API = "http://localhost:8000"
KIT = "/home/tetra/projects/dittobench-starter-kit"
BIN = f"{KIT}/target/release/dittobench-miner"
TMP = "/tmp/claude-1000"
RUN_SIZE = os.environ.get("SWEEP_RUN_SIZE", "full")
WORKERS = int(os.environ.get("SWEEP_WORKERS", "5"))
SEEDS = [int(s) for s in os.environ["SWEEP_SEEDS"].split(",")]
OUT = os.environ.get("SWEEP_OUT", f"{TMP}/sweep-results.json")

lock = threading.Lock()
results = []


def env_for_harness(db):
    env = dict(os.environ)
    # Load the kit's .env (provider/model/keys) without clobbering the DB path.
    with open(f"{KIT}/.env") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    env["DITTOBENCH_DB"] = db
    return env


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


def wait_health(port, proc, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=5):
                return True
        except Exception:
            time.sleep(1)
    return False


def run_seed(seed, port):
    db = f"{TMP}/sweep-{port}.db"
    for suffix in ("", "-shm", "-wal"):
        try:
            os.remove(db + suffix)
        except FileNotFoundError:
            pass
    log = open(f"{TMP}/sweep-h{port}.log", "w")
    proc = subprocess.Popen(
        [BIN, "serve", "--port", str(port)],
        env=env_for_harness(db),
        stdout=log,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    rec = {"seed": seed, "port": port}
    try:
        if not wait_health(port, proc):
            rec["error"] = "harness failed to start"
            return rec
        started = time.time()
        sub = post(
            "/v1/submit",
            {
                "harness_url": f"http://localhost:{port}",
                "run_size": RUN_SIZE,
                "seed": seed,
            },
        )
        rid = sub.get("run_id")
        if not rid:
            rec["error"] = f"no run_id: {sub}"
            return rec
        rec["run_id"] = rid
        status = None
        while time.time() - started < 3600:
            time.sleep(20)
            try:
                d = get(f"/v1/runs/{rid}")
            except Exception as exc:
                rec.setdefault("poll_errors", 0)
                rec["poll_errors"] += 1
                continue
            status = d.get("status")
            if status in ("done", "completed", "failed", "error"):
                break
        rec["status"] = status
        rec["elapsed_sec"] = round(time.time() - started, 1)
        d = get(f"/v1/runs/{rid}")
        report = d.get("report") or {}
        det = report.get("details") or {}
        rec["composite"] = report.get("composite")
        rec["tool_mean"] = report.get("tool_mean")
        rec["memory_mean"] = report.get("memory_mean")
        rec["n"] = report.get("n")
        rec["transform_robustness"] = det.get("transform_robustness")
        rec["audit_case_count"] = det.get("audit_case_count")
        rec["metamorphic_consistency"] = det.get("metamorphic_consistency")
        rec["audit_pairs"] = det.get("audit_pairs")
        rec["error_detail"] = d.get("error")
        # Keep the per-case rows for audit pairs so a surprising aggregate is
        # debuggable after the fact.
        pairs = []
        for cs in report.get("per_case") or []:
            tg = cs.get("twin_group") or ""
            if tg.startswith("auditxf-"):
                pairs.append(
                    {
                        "case_id": cs.get("case_id"),
                        "twin_group": tg,
                        "correct": cs.get("correct"),
                        "score": cs.get("score"),
                        "category": cs.get("category"),
                    }
                )
        rec["audit_cases"] = pairs
        return rec
    except Exception as exc:
        rec["error"] = repr(exc)
        return rec
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=30)
        except Exception:
            pass
        log.close()
        for suffix in ("", "-shm", "-wal"):
            try:
                os.remove(db + suffix)
            except FileNotFoundError:
                pass


def worker(q, port):
    while True:
        try:
            seed = q.get_nowait()
        except queue.Empty:
            return
        rec = run_seed(seed, port)
        with lock:
            results.append(rec)
            tr = rec.get("transform_robustness")
            print(
                f"[seed {seed}] status={rec.get('status')} "
                f"tr={tr} pairs={rec.get('audit_case_count')} "
                f"mc={rec.get('metamorphic_consistency')} "
                f"mem={rec.get('memory_mean')} "
                f"{rec.get('elapsed_sec')}s {rec.get('error') or ''}",
                flush=True,
            )
            with open(OUT, "w") as fh:
                json.dump(results, fh, indent=2)
        q.task_done()


def main():
    q = queue.Queue()
    for s in SEEDS:
        q.put(s)
    threads = []
    for i in range(WORKERS):
        t = threading.Thread(target=worker, args=(q, 8100 + i), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(2)
    for t in threads:
        t.join()
    print(f"\nwrote {OUT} ({len(results)} runs)")


if __name__ == "__main__":
    main()
