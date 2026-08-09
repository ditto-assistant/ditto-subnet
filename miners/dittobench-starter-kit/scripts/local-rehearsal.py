#!/usr/bin/env python3
"""Run the starter harness through the local DittoBench v8 practice validator."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
KIT_DIR = SCRIPT_DIR.parent
REPO_ROOT = KIT_DIR.parents[1]
API_DIR = REPO_ROOT / "services" / "dittobench-api"
ACTIVE_BENCH_VERSION = 8
TERMINAL_STATUSES = {"done", "failed"}
SECRET_PATTERNS = (
    re.compile(r"sk-or-v1-[A-Za-z0-9_-]+"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
)


class RehearsalError(RuntimeError):
    """An actionable local-rehearsal failure."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the starter harness and local DittoBench API, run a v8 "
            "rehearsal with validator-observed tools, print the report, and "
            "tear both processes down."
        )
    )
    parser.add_argument(
        "--run-size",
        choices=("small", "medium", "full"),
        default="small",
        help="v8 profile to run (default: small smoke profile)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        help="pin the generated dataset; omit for a fresh random seed",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=7200,
        help="maximum scoring time in seconds (default: 7200)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="also write the completed run envelope to this JSON file",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def require_command(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RehearsalError(
            f"required command is not installed or not on PATH: {name}"
        )
    return path


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def reserve_ports(count: int) -> list[int]:
    ports: set[int] = set()
    while len(ports) < count:
        ports.add(reserve_port())
    return list(ports)


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            decoded = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RehearsalError(
            f"{method} {url} returned HTTP {exc.code}: {detail}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RehearsalError(f"{method} {url} failed: {exc}") from exc
    if not isinstance(decoded, dict):
        raise RehearsalError(f"{method} {url} returned a non-object JSON response")
    return decoded


def wait_for_health(
    base_url: str,
    process: subprocess.Popen[bytes],
    *,
    timeout: float = 300,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RehearsalError(
                f"process exited with status {process.returncode} before "
                f"{base_url}/health was ready"
            )
        try:
            health = request_json(f"{base_url}/health", timeout=1)
            if health.get("status") == "ok":
                return
        except RehearsalError:
            pass
        time.sleep(0.25)
    raise RehearsalError(f"timed out waiting for {base_url}/health")


def submit_body(run_size: str, harness_url: str, seed: int | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "bench_version": ACTIVE_BENCH_VERSION,
        "harness_url": harness_url,
        "run_size": run_size,
    }
    if seed is not None:
        body["seed"] = seed
    return body


def poll_run(api_url: str, run_id: str, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_progress: tuple[Any, Any, Any] | None = None
    while time.monotonic() < deadline:
        run = request_json(f"{api_url}/v1/runs/{run_id}")
        status = run.get("status")
        progress = run.get("progress") or {}
        current = (progress.get("stage"), progress.get("done"), progress.get("total"))
        if current != last_progress:
            stage, done, total = current
            if stage:
                print(f"{stage}: {done or 0}/{total or 0}", flush=True)
            last_progress = current
        if status in TERMINAL_STATUSES:
            return run
        time.sleep(1)
    raise RehearsalError(f"run {run_id} did not finish within {int(timeout)} seconds")


def format_summary(run: dict[str, Any]) -> str:
    report = run.get("report") or {}
    details = report.get("details") or {}
    lines = [
        "",
        f"=== DittoBench local v8 rehearsal ({run.get('run_id', 'unknown')}) ===",
        f"seed:                {report.get('seed', run.get('seed', 'unknown'))}",
        f"dataset_sha256:      {details.get('dataset_sha256', 'unknown')}",
        f"composite:           {float(report.get('composite', 0)):.3f}",
        f"tool_mean:           {float(report.get('tool_mean', 0)):.3f}",
        f"memory_mean:         {float(report.get('memory_mean', 0)):.3f}",
        f"observed_tool_cases: {details.get('observed_tool_cases', 0)}",
        f"capped_tool_cases:   {details.get('capped_tool_cases', 0)}",
        "",
        "This run used the real local v8 generator, staged seeding, scorer, and",
        "validator-visible tool endpoint. It is still a rehearsal, not submission",
        "certification: chat and embeddings came from your local .env, and no",
        "screened container or ticket-bound platform inference was used.",
    ]
    return "\n".join(lines)


def redact(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(r"\1[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


def log_tail(path: Path, lines: int = 80) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return redact("\n".join(content[-lines:]))


def stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def run(args: argparse.Namespace) -> int:
    require_command("cargo")
    require_command("go")
    if not API_DIR.is_dir():
        raise RehearsalError(
            "local rehearsal needs the ditto-subnet monorepo checkout; "
            f"missing {API_DIR}"
        )

    with tempfile.TemporaryDirectory(prefix="dittobench-local-rehearsal-") as raw_tmp:
        tmp = Path(raw_tmp)
        api_binary = tmp / "dittobench-api"
        harness_log = tmp / "harness.log"
        api_log = tmp / "api.log"
        harness_port, api_port, broker_port = reserve_ports(3)
        harness_url = f"http://127.0.0.1:{harness_port}"
        api_url = f"http://127.0.0.1:{api_port}"

        print("building the starter harness and local v8 scorer...", flush=True)
        subprocess.run(
            ["cargo", "build", "--locked", "--bin", "dittobench-miner"],
            cwd=KIT_DIR,
            check=True,
        )
        metadata = subprocess.run(
            ["cargo", "metadata", "--no-deps", "--format-version", "1"],
            cwd=KIT_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
        target_dir = Path(json.loads(metadata.stdout)["target_directory"])
        harness_binary = target_dir / "debug" / "dittobench-miner"
        subprocess.run(
            ["go", "build", "-o", str(api_binary), "./cmd/dittobench-api"],
            cwd=API_DIR,
            check=True,
        )

        harness_env = os.environ.copy()
        # A canonical validator starts every submission with a fresh store. Keep
        # local rehearsals equally isolated from seed-user and previous runs.
        harness_env["DITTOBENCH_DB"] = str(tmp / "rehearsal.db")
        api_env = os.environ.copy()
        api_env["DITTOBENCH_ALLOW_PRIVATE_HARNESS"] = "1"
        api_env["DITTOBENCH_BROKER_PORT"] = str(broker_port)
        # One case at a time is friendlier to a laptop-sized local Ollama while
        # preserving deterministic score semantics (latency is not scored).
        api_env.setdefault("DITTOBENCH_V8_CASE_CONCURRENCY", "1")

        harness: subprocess.Popen[bytes] | None = None
        api: subprocess.Popen[bytes] | None = None
        with harness_log.open("wb") as harness_output, api_log.open("wb") as api_output:
            try:
                harness = subprocess.Popen(
                    [str(harness_binary), "serve", "--port", str(harness_port)],
                    cwd=KIT_DIR,
                    env=harness_env,
                    stdout=harness_output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                api = subprocess.Popen(
                    [str(api_binary), "-port", str(api_port)],
                    cwd=API_DIR,
                    env=api_env,
                    stdout=api_output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                print("waiting for the harness and scorer...", flush=True)
                wait_for_health(harness_url, harness)
                wait_for_health(api_url, api)

                accepted = request_json(
                    f"{api_url}/v1/submit",
                    method="POST",
                    payload=submit_body(args.run_size, harness_url, args.seed),
                )
                run_id = accepted.get("run_id")
                if not isinstance(run_id, str) or not run_id:
                    raise RehearsalError(f"submit response omitted run_id: {accepted}")
                print(
                    f"running v8 {args.run_size} profile (run {run_id})...",
                    flush=True,
                )
                completed = poll_run(api_url, run_id, timeout=args.timeout)
                if completed.get("status") != "done":
                    failure = completed.get("failure") or {}
                    detail = (
                        completed.get("error")
                        or failure.get("code")
                        or "unknown failure"
                    )
                    raise RehearsalError(f"local scorer failed run {run_id}: {detail}")
                print(format_summary(completed))
                if args.report is not None:
                    report_path = args.report.expanduser().resolve()
                    report_path.parent.mkdir(parents=True, exist_ok=True)
                    report_path.write_text(
                        json.dumps(completed, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    print(f"wrote report: {report_path}")
                return 0
            except (subprocess.CalledProcessError, RehearsalError) as exc:
                print(f"local rehearsal failed: {exc}", file=sys.stderr)
                for label, path in (("harness", harness_log), ("scorer", api_log)):
                    tail = log_tail(path)
                    if tail:
                        print(f"\n--- {label} log tail ---\n{tail}", file=sys.stderr)
                return 1
            finally:
                stop_process(api)
                stop_process(harness)


def main() -> int:
    try:
        return run(parse_args())
    except (subprocess.CalledProcessError, RehearsalError) as exc:
        print(f"local rehearsal failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
