#!/usr/bin/env python3
"""Frozen campaign resume, dry-run by default; --execute needs operator approval.

Portable counterpart of private wrapper SHA-256
c0c8bf1a746361c26258f3f8d840bdfd88a349974a8a32c65464ae9fa122ae69.
It adds explicit non-assert guards and pins the exact public resume cohort.
No cache cleanup, process termination, or automatic scheduling is performed.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SOURCE = "1b5755609b3ea95660fdba289e6a747adb8c5dae"
BINARY_SHA = "695b6c3a0338b902949deb55456a87b17e1ba97e2b92dca63054e187ef75fa21"
MANIFEST_SHA = "a20eec7a9d772e153403b279faa919e0e24b9ea57fa2f9dc1ab2d6d0636f3095"
COHORT_SHA = "8e69fb9c3d07f97db9902619c48c5f39040c5046a028f34998cf74a415ad348c"
CONFIG_HASHES = {".env.local": "d2bee32f9749c311831f29637469c6633145ba38379b874e2d891dace2d03377",
                 ".env.common": "6fb161f7c7b714ad851a2242ed0f9987a895321817a1de2c73f244368f54081f"}
DATABASE = "postgresql://postgres:postgres@127.0.0.1:54339/ditto_lme_s_final"
MIN_AVAILABLE_KIB = 8 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_users(record):
    users = record.get("remaining_native_users")
    require(isinstance(users, list) and all(isinstance(u, str) for u in users), "invalid resume cohort")
    require(len(users) == len(set(users)) == 169, "resume cohort cardinality differs")
    encoded = json.dumps(users, separators=(",", ":"), ensure_ascii=True).encode()
    require(hashlib.sha256(encoded).hexdigest() == COHORT_SHA, "resume cohort differs from the frozen selection")
    return users


def inject_config(runtime, text):
    # Same literal parsing/order as cfg/envs.EnvFile.Load for valid env files.
    for line in text.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        require(key and "\x00" not in key and "\x00" not in value, "invalid configuration key/value")
        runtime.setdefault(key, value)


def verify(base, graph, adc, selection):
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=graph, text=True).strip()
    require(source == SOURCE, "source revision differs from frozen build")
    require(not subprocess.check_output(["git", "status", "--porcelain"], cwd=graph, text=True).strip(), "source checkout is dirty")
    binary = graph / ".tmp/lme/dittobench-campaign-vcs-1b575560"
    manifest = base / ".tmp/lme/seed_manifest_receipts_wave2.json"
    output = base / ".tmp/lme/evidence/dream-resumed-1b575560-after-disk-stop.jsonl"
    require(digest(binary) == BINARY_SHA, "binary digest differs")
    require(digest(manifest) == MANIFEST_SHA, "manifest digest differs")
    require(not output.exists(), "output exists; another interruption requires a newly reviewed selection/output")
    require(adc.is_file(), "explicit working ADC file is required")
    users = checked_users(json.loads(selection.read_text()))
    runtime = dict(os.environ)
    for filename, expected in CONFIG_HASHES.items():
        path = base / "cfg/envs" / filename
        require(digest(path) == expected, "configuration digest differs")
        inject_config(runtime, path.read_text())
    require(runtime.get("GCLOUD_PROJECT"), "project configuration missing; embedded-config fallback is not allowed")
    runtime["SUPABASE_DB_URL"] = DATABASE
    runtime["GOOGLE_APPLICATION_CREDENTIALS"] = str(adc)
    runtime["DITTO_ENV"] = "local"
    command = [str(binary), "-env", "local", "longmemeval-dream", "-stage", "dream",
               "-manifest", str(manifest), "-users", ",".join(users), "-concurrency", "8", "-out", str(output)]
    return command, runtime


def require_stable_headroom(base):
    for sample in range(3):
        raw = subprocess.check_output(["df", "-k", str(base)], text=True)
        available = int(raw.splitlines()[-1].split()[3])
        print(f"available_kib={available}", flush=True)
        require(available >= MIN_AVAILABLE_KIB, "insufficient headroom; no inference started")
        if sample < 2:
            time.sleep(30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path, help="preserved task backend clone")
    parser.add_argument("--graph", required=True, type=Path, help="frozen task backend-graph checkout")
    parser.add_argument("--adc", type=Path, default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"), help="explicit working ADC file")
    parser.add_argument("--selection", type=Path, default=Path(__file__).resolve().parents[2] / "docs/longmemeval-benchmark/results/2026-09-12-ditto-resource-pause-221310.json")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="default: identity/cohort/config validation only")
    action.add_argument("--execute", action="store_true", help="ONLY after explicit operator approval; requires three fresh >=8 GiB samples")
    args = parser.parse_args()
    require(args.adc is not None, "provide --adc or GOOGLE_APPLICATION_CREDENTIALS")
    base, graph, adc = args.base.resolve(), args.graph.resolve(), Path(args.adc).resolve()
    verify(base, graph, adc, args.selection)
    if not args.execute:
        print("Verified frozen source, binary, manifest, 169-user cohort, configuration digests and ADC path. Dry run only; no inference.")
        return
    require_stable_headroom(base)
    command, runtime = verify(base, graph, adc, args.selection)
    os.chdir(graph)
    os.execve(command[0], command, runtime)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
        print(f"resume refused: {exc}", file=sys.stderr)
        sys.exit(1)
