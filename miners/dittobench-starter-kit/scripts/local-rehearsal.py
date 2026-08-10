#!/usr/bin/env python3
"""Run a miner harness through self-contained local Bench v9 practice."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
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
DEFAULT_KIT_DIR = SCRIPT_DIR.parent
REPO_ROOT = DEFAULT_KIT_DIR.parents[1]
API_DIR = REPO_ROOT / "services" / "dittobench-api"
LONGMEM_DIR = API_DIR / "integrations" / "longmemeval"
ACTIVE_BENCH_VERSION = 9
LONGMEM_DATASET_SHA256 = (
    "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
)
LONGMEM_DATASET_REVISION = "98d7416c24c778c2fee6e6f3006e7a073259d48f"
LONGMEM_DATASET_URL = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/"
    f"{LONGMEM_DATASET_REVISION}/longmemeval_s_cleaned.json"
)
LONGMEM_SOURCE_REVISION = "9e0b455f4ef0e2ab8f2e582289761153549043fc"
LONGMEM_JUDGE_IMAGE = f"ditto-longmemeval-judge:{LONGMEM_SOURCE_REVISION[:12]}"
LONGMEM_CONDITION = "longmemeval-s-cleaned-native-memory-tools-v2"
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
            "Build the starter harness and local DittoBench API, run a v9 "
            "rehearsal with validator-observed tools, print the report, and "
            "optionally append the separate LongMemEval-S score."
        )
    )
    parser.add_argument(
        "--starter-kit",
        type=Path,
        default=DEFAULT_KIT_DIR,
        help="starter-kit checkout to build and test",
    )
    parser.add_argument(
        "--run-size",
        choices=("small", "medium", "full"),
        default="small",
        help="v9 profile to run (default: small smoke profile)",
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
        help="write the combined Bench and optional LongMemEval report as JSON",
    )
    parser.add_argument(
        "--longmem-eval",
        action="store_true",
        help="also run the pinned native-memory-tools LongMemEval-S condition",
    )
    parser.add_argument(
        "--longmem-limit",
        type=int,
        help="limit LongMemEval questions; omit for the official 500-question set",
    )
    parser.add_argument(
        "--longmem-shards",
        type=int,
        default=5,
        help="isolated harness stores for LongMemEval (default: 5, max: 10)",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if not 1 <= args.longmem_shards <= 10:
        parser.error("--longmem-shards must be between 1 and 10")
    if args.longmem_limit is not None and not 1 <= args.longmem_limit <= 500:
        parser.error("--longmem-limit must be between 1 and 500")
    if args.longmem_limit is not None and not args.longmem_eval:
        parser.error("--longmem-limit requires --longmem-eval")
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


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{secrets.token_hex(8)}")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
        f"=== DittoBench local v9 rehearsal ({run.get('run_id', 'unknown')}) ===",
        f"seed:                {report.get('seed', run.get('seed', 'unknown'))}",
        f"dataset_sha256:      {details.get('dataset_sha256', 'unknown')}",
        f"composite:           {float(report.get('composite', 0)):.3f}",
        f"tool_mean:           {float(report.get('tool_mean', 0)):.3f}",
        f"memory_mean:         {float(report.get('memory_mean', 0)):.3f}",
        f"observed_tool_cases: {details.get('observed_tool_cases', 0)}",
        f"capped_tool_cases:   {details.get('capped_tool_cases', 0)}",
        "",
        "This run used the real local v9 generator, staged seeding, scorer, and",
        "validator-visible tool endpoint. It is still a rehearsal, not submission",
        "certification: chat and embeddings came from your local .env, and no",
        "screened container or ticket-bound platform inference was used.",
    ]
    return "\n".join(lines)


def format_longmem_summary(result: dict[str, Any]) -> str:
    scope = (
        "official full condition"
        if result["official_full_condition"]
        else "partial practice"
    )
    return "\n".join(
        [
            "",
            f"=== LongMemEval-S ({scope}) ===",
            f"condition: {result['condition']}",
            f"correct:   {result['correct']}/{result['n']}",
            f"accuracy:  {result['accuracy']:.6f}",
            f"abstention:{result['abstention_correct']}/{result['abstention_n']}",
            "",
            "LongMemEval is a separate offline adapter score. It is not folded",
            "into the Bench v9 composite, KOTH rank, confirmation, or payout.",
        ]
    )


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def longmem_dataset_cache_path() -> Path:
    cache_root = Path(
        os.environ.get("DITTO_CACHE_DIR", str(Path.home() / ".cache" / "ditto"))
    )
    return (
        cache_root
        / "longmemeval"
        / LONGMEM_DATASET_REVISION
        / "longmemeval_s_cleaned.json"
    )


def ensure_longmem_dataset() -> Path:
    destination = longmem_dataset_cache_path()
    if destination.is_file() and sha256_file(destination) == LONGMEM_DATASET_SHA256:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_suffix(f".download-{secrets.token_hex(8)}")
    print("downloading the pinned cleaned LongMemEval-S dataset...", flush=True)
    try:
        request = urllib.request.Request(
            LONGMEM_DATASET_URL,
            headers={"User-Agent": "ditto-practice/1"},
        )
        with (
            urllib.request.urlopen(request, timeout=300) as response,
            temporary.open("wb") as output,
        ):
            shutil.copyfileobj(response, output, length=1024 * 1024)
        if sha256_file(temporary) != LONGMEM_DATASET_SHA256:
            raise RehearsalError(
                "downloaded LongMemEval dataset failed SHA-256 verification"
            )
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        return destination
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise RehearsalError(f"download LongMemEval dataset failed: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def sanitized_build_env() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("OPENROUTER_API_KEY", "CHUTES_API_KEY"):
        environment.pop(name, None)
    return environment


def scorer_environment(tmp: Path, broker_port: int) -> dict[str, str]:
    """Build the local scorer environment without inheriting provider secrets."""
    private_dir = tmp / "private-projections"
    artifact_dir = tmp / "artifacts"
    private_dir.mkdir(mode=0o700)
    private_dir.chmod(0o700)
    artifact_dir.mkdir(mode=0o700)
    artifact_dir.chmod(0o700)

    environment = os.environ.copy()
    environment.pop("OPENROUTER_API_KEY", None)
    environment.pop("CHUTES_API_KEY", None)
    environment["DITTOBENCH_ALLOW_PRIVATE_HARNESS"] = "1"
    environment["DITTOBENCH_BROKER_PORT"] = str(broker_port)
    environment["DITTOBENCH_PRIVATE_ARTIFACT_DIR"] = str(private_dir)
    environment["DITTOBENCH_ARTIFACT_DIR"] = str(artifact_dir)
    # One case at a time is friendlier to a laptop-sized local Ollama while
    # preserving deterministic score semantics (latency is not scored).
    environment.setdefault("DITTOBENCH_V8_CASE_CONCURRENCY", "1")
    return environment


def longmem_judge_context() -> str:
    return (
        "https://github.com/xiaowu0162/LongMemEval.git"
        f"?ref=refs/heads/main&checksum={LONGMEM_SOURCE_REVISION}"
    )


def ensure_longmem_judge_image() -> None:
    inspected = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
            LONGMEM_JUDGE_IMAGE,
        ],
        stdout=subprocess.PIPE,
        text=True,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if (
        inspected.returncode == 0
        and inspected.stdout.strip() == LONGMEM_SOURCE_REVISION
    ):
        return
    dockerfile = (LONGMEM_DIR / "Dockerfile.judge").read_bytes()
    print("building the pinned official LongMemEval judge image...", flush=True)
    subprocess.run(
        [
            "docker",
            "build",
            "--file",
            "-",
            "--label",
            f"org.opencontainers.image.revision={LONGMEM_SOURCE_REVISION}",
            "--tag",
            LONGMEM_JUDGE_IMAGE,
            longmem_judge_context(),
        ],
        input=dockerfile,
        env=sanitized_build_env(),
        check=True,
    )


def start_process(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    log_path: Path,
) -> tuple[subprocess.Popen[bytes], Any]:
    output = log_path.open("wb")
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception:
        output.close()
        raise
    return process, output


def summarize_longmem(evaluation_path: Path, *, limit: int | None) -> dict[str, Any]:
    rows = [
        json.loads(line)
        for line in evaluation_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    expected = 500 if limit is None else limit
    ids = [row.get("question_id") for row in rows]
    if (
        len(rows) != expected
        or len(set(ids)) != expected
        or not all(isinstance(value, str) and value for value in ids)
    ):
        raise RehearsalError(
            "official LongMemEval judge returned "
            f"{len(rows)} unique rows; expected {expected}"
        )
    labels: list[bool] = []
    abstention: list[bool] = []
    for row in rows:
        autoeval = row.get("autoeval_label")
        if (
            not isinstance(autoeval, dict)
            or autoeval.get("model") != "gpt-4o-2024-08-06"
        ):
            raise RehearsalError(
                "LongMemEval output is not from the pinned official judge"
            )
        label = autoeval.get("label")
        if not isinstance(label, bool):
            raise RehearsalError("LongMemEval judge emitted a non-boolean label")
        labels.append(label)
        if str(row["question_id"]).endswith("_abs"):
            abstention.append(label)
    return {
        "schema_version": 1,
        "condition": LONGMEM_CONDITION,
        "bench_version": ACTIVE_BENCH_VERSION,
        "dataset_sha256": LONGMEM_DATASET_SHA256,
        "dataset_revision": LONGMEM_DATASET_REVISION,
        "source_revision": LONGMEM_SOURCE_REVISION,
        "reader_model": "openai/gpt-4.1",
        "reader_profile": "longmemeval-openrouter-gpt41-reader-v1",
        "judge_model": "gpt-4o-2024-08-06",
        "judge_profile": "longmemeval-official-gpt4o-openrouter-v1",
        "n": len(labels),
        "correct": sum(labels),
        "accuracy": round(sum(labels) / len(labels), 6),
        "abstention_n": len(abstention),
        "abstention_correct": sum(abstention),
        "official_full_condition": limit is None and len(labels) == 500,
        "evaluation_sha256": sha256_file(evaluation_path),
    }


def longmem_adapter_command(
    *,
    args: argparse.Namespace,
    kit_dir: Path,
    dataset: Path,
    harness_urls: list[str],
    hypotheses: Path,
    manifest: Path,
    user_id_namespace: str,
) -> list[str]:
    command = [
        sys.executable,
        str(LONGMEM_DIR / "longmemeval_adapter.py"),
        "--dataset",
        str(dataset),
        "--dataset-sha256",
        LONGMEM_DATASET_SHA256,
        "--harness-urls",
        ",".join(harness_urls),
        "--output",
        str(hypotheses),
        "--manifest",
        str(manifest),
        "--agent-label",
        kit_dir.name,
        "--bench-version",
        str(ACTIVE_BENCH_VERSION),
        "--retrieval-mode",
        "native-memory-tools",
        "--answer-model",
        "openai/gpt-4.1",
        "--answer-model-profile",
        "longmemeval-openrouter-gpt41-reader-v1",
        "--user-id-namespace",
        user_id_namespace,
    ]
    if args.longmem_limit is not None:
        command.extend(("--limit", str(args.longmem_limit)))
    return command


def run_longmem(
    *,
    args: argparse.Namespace,
    kit_dir: Path,
    harness_binary: Path,
    tmp: Path,
) -> dict[str, Any]:
    require_command("docker")
    if not os.environ.get("OPENROUTER_API_KEY", "").strip():
        raise RehearsalError(
            "--longmem-eval requires OPENROUTER_API_KEY in the exported environment; "
            "the value remains only in the trusted local reader and judge proxies"
        )
    dataset = ensure_longmem_dataset()
    ensure_longmem_judge_image()
    reader_port, judge_port, *harness_ports = reserve_ports(2 + args.longmem_shards)
    reader_url = f"http://127.0.0.1:{reader_port}"
    judge_url = f"http://127.0.0.1:{judge_port}"
    processes: list[tuple[str, subprocess.Popen[bytes], Path, Any]] = []
    try:
        proxy_env = os.environ.copy()
        proxy_env["PORT"] = str(reader_port)
        reader_log = tmp / "longmem-reader.log"
        reader, reader_output = start_process(
            [sys.executable, str(LONGMEM_DIR / "openrouter_reader_proxy.py")],
            cwd=LONGMEM_DIR,
            environment=proxy_env,
            log_path=reader_log,
        )
        processes.append(("longmem reader", reader, reader_log, reader_output))
        wait_for_health(reader_url, reader)

        harness_urls: list[str] = []
        for shard, port in enumerate(harness_ports, 1):
            harness_env = os.environ.copy()
            harness_env.pop("OPENROUTER_API_KEY", None)
            harness_env["DITTOBENCH_PROVIDER"] = "chutes"
            harness_env["DITTOBENCH_MODEL"] = "openai/gpt-4.1"
            harness_env["DITTOBENCH_INFERENCE_BASE_URL"] = reader_url + "/v1"
            harness_env["CHUTES_BASE_URL"] = reader_url + "/v1"
            harness_env["CHUTES_API_KEY"] = "local-longmemeval-non-secret"
            harness_env["DITTOBENCH_DB"] = str(tmp / f"longmem-shard-{shard}.db")
            harness_log = tmp / f"longmem-harness-{shard}.log"
            harness, harness_output = start_process(
                [str(harness_binary), "serve", "--port", str(port)],
                cwd=kit_dir,
                environment=harness_env,
                log_path=harness_log,
            )
            processes.append(
                (f"longmem harness {shard}", harness, harness_log, harness_output)
            )
            harness_url = f"http://127.0.0.1:{port}"
            wait_for_health(harness_url, harness)
            harness_urls.append(harness_url)

        hypotheses = tmp / "longmemeval-hypotheses.jsonl"
        manifest = tmp / "longmemeval-manifest.json"
        adapter_command = longmem_adapter_command(
            args=args,
            kit_dir=kit_dir,
            dataset=dataset,
            harness_urls=harness_urls,
            hypotheses=hypotheses,
            manifest=manifest,
            user_id_namespace=secrets.token_hex(16),
        )
        print(
            f"running LongMemEval-S on {args.longmem_limit or 500} questions "
            f"across {args.longmem_shards} isolated stores...",
            flush=True,
        )
        subprocess.run(adapter_command, cwd=REPO_ROOT, check=True)

        judge_env = os.environ.copy()
        judge_env["PORT"] = str(judge_port)
        judge_log = tmp / "longmem-judge.log"
        judge, judge_output = start_process(
            [sys.executable, str(LONGMEM_DIR / "openrouter_judge_proxy.py")],
            cwd=LONGMEM_DIR,
            environment=judge_env,
            log_path=judge_log,
        )
        processes.append(("longmem judge", judge, judge_log, judge_output))
        wait_for_health(judge_url, judge)

        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--add-host",
                "host.docker.internal:host-gateway",
                "--env",
                "OPENAI_API_KEY=local-longmemeval-non-secret",
                "--env",
                f"OPENAI_BASE_URL=http://host.docker.internal:{judge_port}/v1",
                "--mount",
                f"type=bind,src={tmp},dst=/results",
                "--mount",
                f"type=bind,src={dataset},dst=/data/longmemeval_s_cleaned.json,readonly",
                LONGMEM_JUDGE_IMAGE,
                "gpt-4o",
                "/results/longmemeval-hypotheses.jsonl",
                "/data/longmemeval_s_cleaned.json",
            ],
            env=sanitized_build_env(),
            check=True,
        )
        evaluation = tmp / "longmemeval-hypotheses.jsonl.eval-results-gpt-4o"
        result = summarize_longmem(evaluation, limit=args.longmem_limit)
        result["hypotheses_sha256"] = sha256_file(hypotheses)
        result["manifest_sha256"] = sha256_file(manifest)
        return result
    except (subprocess.CalledProcessError, RehearsalError):
        for label, _, log_path, _ in processes:
            tail = log_tail(log_path)
            if tail:
                print(f"\n--- {label} log tail ---\n{tail}", file=sys.stderr)
        raise
    finally:
        for _, process, _, output in reversed(processes):
            stop_process(process)
            output.close()


def run(args: argparse.Namespace) -> int:
    require_command("cargo")
    require_command("go")
    kit_dir = args.starter_kit.expanduser().resolve()
    if not (kit_dir / "Cargo.toml").is_file():
        raise RehearsalError(f"starter kit must contain Cargo.toml: {kit_dir}")
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

        print("building the miner harness and local v9 scorer...", flush=True)
        subprocess.run(
            ["cargo", "build", "--locked", "--bin", "dittobench-miner"],
            cwd=kit_dir,
            env=sanitized_build_env(),
            check=True,
        )
        metadata = subprocess.run(
            ["cargo", "metadata", "--no-deps", "--format-version", "1"],
            cwd=kit_dir,
            env=sanitized_build_env(),
            check=True,
            capture_output=True,
            text=True,
        )
        target_dir = Path(json.loads(metadata.stdout)["target_directory"])
        harness_binary = target_dir / "debug" / "dittobench-miner"
        subprocess.run(
            ["go", "build", "-o", str(api_binary), "./cmd/dittobench-api"],
            cwd=API_DIR,
            env=sanitized_build_env(),
            check=True,
        )

        harness_env = os.environ.copy()
        # A canonical validator starts every submission with a fresh store. Keep
        # local rehearsals equally isolated from seed-user and previous runs.
        harness_env["DITTOBENCH_DB"] = str(tmp / "rehearsal.db")
        api_env = scorer_environment(tmp, broker_port)

        harness: subprocess.Popen[bytes] | None = None
        api: subprocess.Popen[bytes] | None = None
        with harness_log.open("wb") as harness_output, api_log.open("wb") as api_output:
            try:
                harness = subprocess.Popen(
                    [str(harness_binary), "serve", "--port", str(harness_port)],
                    cwd=kit_dir,
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
                    f"running v9 {args.run_size} profile (run {run_id})...",
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
                combined = dict(completed)
                report_path = (
                    args.report.expanduser().resolve()
                    if args.report is not None
                    else None
                )
                if report_path is not None:
                    # Preserve the completed Bench report even if the optional,
                    # separately scored research adapter later fails.
                    write_report(report_path, combined)
                if args.longmem_eval:
                    stop_process(api)
                    api = None
                    stop_process(harness)
                    harness = None
                    longmem = run_longmem(
                        args=args,
                        kit_dir=kit_dir,
                        harness_binary=harness_binary,
                        tmp=tmp,
                    )
                    combined["longmemeval"] = longmem
                    print(format_longmem_summary(longmem))
                    if report_path is not None:
                        write_report(report_path, combined)
                if report_path is not None:
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
