"""Operator Cloud Run probes for the three screening lanes.

Kaniko Jobs, an internal-or-public smoke Service, and a source-review Job.
Tokens come from gcloud; they are never printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from uuid import uuid4

from screener_capacity.targon_cli import (
    _load_source_review_api_key,
    _source_review_starter_kit_mock_script,
)
from screener_capacity.targon_screen_contract import starter_kit_rental_script

_PROJECT = os.environ.get("CLOUDRUN_PROBE_PROJECT", "ditto-app-dev")
_REGION = os.environ.get("CLOUDRUN_PROBE_REGION", "us-central1")
_UNTRUSTED_SA = os.environ.get(
    "CLOUDRUN_PROBE_SA",
    "ditto-screening-untrusted@ditto-app-dev.iam.gserviceaccount.com",
)
_KANIKO = (
    "us-central1-docker.pkg.dev/ditto-app-dev/"
    "ditto-public-builders/kaniko-executor:v1.25.16"
)
_STARTER_SHA = "65f9e2cb761a4db4ae71493b9a483870bf48ed7b"


def _gcloud(*args: str, timeout: int = 120) -> str:
    env = os.environ.copy()
    env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
    completed = subprocess.run(
        ["gcloud", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        lines = [line for line in err.splitlines() if "Bearer " not in line]
        raise RuntimeError("\n".join(lines[-12:]) if lines else "gcloud failed")
    return completed.stdout


def _identity_token(audience: str) -> str:
    del audience
    token = _gcloud(
        "auth",
        "print-identity-token",
        "--account=peyton@omniaura.ai",
    ).strip()
    if len(token) < 20:
        raise RuntimeError("identity token was empty")
    return token


def command_kaniko_probe(args: argparse.Namespace) -> int:
    job = f"ditto-probe-kaniko-{secrets.token_hex(3)}"
    script = starter_kit_rental_script(
        source_sha=args.starter_kit_sha,
        agent_id=str(uuid4()),
        attempt_id=str(uuid4()),
        hold_seconds=5,
    )
    print(json.dumps({"phase": "create-job", "job": job}))
    try:
        _gcloud(
            "run",
            "jobs",
            "create",
            job,
            f"--project={_PROJECT}",
            f"--region={_REGION}",
            f"--image={_KANIKO}",
            "--command=/busybox/sh",
            f"--args=^|^-c|{script}",
            "--tasks=1",
            "--max-retries=0",
            f"--task-timeout={int(args.timeout_seconds)}s",
            "--cpu=8",
            "--memory=32Gi",
            f"--service-account={_UNTRUSTED_SA}",
            timeout=180,
        )
        print(json.dumps({"phase": "execute", "job": job}))
        _gcloud(
            "run",
            "jobs",
            "execute",
            job,
            f"--project={_PROJECT}",
            f"--region={_REGION}",
            "--wait",
            timeout=int(args.timeout_seconds) + 180,
        )
        logs = _gcloud(
            "logging",
            "read",
            (
                "resource.type=cloud_run_job AND "
                f"resource.labels.job_name={job}"
            ),
            f"--project={_PROJECT}",
            "--limit=80",
            "--format=value(textPayload)",
            timeout=60,
        )
        available = "KANIKO_STARTER_PROBE_AVAILABLE" in logs
        print(
            json.dumps(
                {
                    "phase": "kaniko",
                    "capability": "AVAILABLE" if available else "UNAVAILABLE",
                    "job": job,
                    "marker": available,
                },
                sort_keys=True,
            )
        )
        return 0 if available else 6
    finally:
        _gcloud(
            "run",
            "jobs",
            "delete",
            job,
            f"--project={_PROJECT}",
            f"--region={_REGION}",
            "--quiet",
            timeout=120,
        )
        print(json.dumps({"phase": "deleted", "job": job}))


def command_runtime_probe(args: argparse.Namespace) -> int:
    service = f"ditto-probe-smoke-{secrets.token_hex(3)}"
    print(json.dumps({"phase": "create-service", "service": service}))
    try:
        _gcloud(
            "run",
            "deploy",
            service,
            f"--project={_PROJECT}",
            f"--region={_REGION}",
            f"--image={args.image}",
            "--port=8080",
            "--cpu=1",
            "--memory=2Gi",
            "--max-instances=1",
            "--timeout=60",
            "--ingress=all",
            "--no-allow-unauthenticated",
            f"--service-account={_UNTRUSTED_SA}",
            "--set-env-vars=OPENROUTER_API_KEY=sk-screener-smoke,DITTOBENCH_DB=/tmp/dittobench.db",
            timeout=300,
        )
        describe = _gcloud(
            "run",
            "services",
            "describe",
            service,
            f"--project={_PROJECT}",
            f"--region={_REGION}",
            "--format=value(status.url)",
        )
        url = describe.strip().rstrip("/")
        if not url.startswith("https://"):
            print(json.dumps({"phase": "runtime-health", "capability": "UNAVAILABLE"}))
            return 6
        token = _identity_token(url)
        req = urllib.request.Request(
            f"{url}/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        deadline = time.monotonic() + args.timeout_seconds
        last_error = "timeout"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    if response.status == 200:
                        print(
                            json.dumps(
                                {
                                    "phase": "runtime-health",
                                    "capability": "AVAILABLE",
                                    "service": service,
                                    "native_health": True,
                                },
                                sort_keys=True,
                            )
                        )
                        return 0
                    last_error = f"http-{response.status}"
            except (OSError, urllib.error.URLError) as error:
                last_error = type(error).__name__
            time.sleep(5)
        print(
            json.dumps(
                {
                    "phase": "runtime-health",
                    "capability": "UNAVAILABLE",
                    "error": last_error,
                },
                sort_keys=True,
            )
        )
        return 6
    finally:
        _gcloud(
            "run",
            "services",
            "delete",
            service,
            f"--project={_PROJECT}",
            f"--region={_REGION}",
            "--quiet",
            timeout=180,
        )
        print(json.dumps({"phase": "deleted", "service": service}))


def command_source_review_probe(args: argparse.Namespace) -> int:
    mock = f"ditto-probe-srmock-{secrets.token_hex(3)}"
    review = f"ditto-probe-srjob-{secrets.token_hex(3)}"
    review_id = str(uuid4())
    attempt_id = str(uuid4())
    job_token = secrets.token_urlsafe(48)
    script = _source_review_starter_kit_mock_script(
        review_id=review_id,
        job_token=job_token,
        archive_url=(
            "https://codeload.github.com/ditto-assistant/dittobench-starter-kit/"
            f"tar.gz/{_STARTER_SHA}"
        ),
    )
    print(json.dumps({"phase": "create-mock", "service": mock}))
    encoded = base64.b64encode(script.encode()).decode()
    wrapper = f"import base64; exec(base64.b64decode({encoded!r}))"
    try:
        _gcloud(
            "run",
            "deploy",
            mock,
            f"--project={_PROJECT}",
            f"--region={_REGION}",
            "--image=python:3.12-alpine",
            "--port=8080",
            "--cpu=1",
            "--memory=1Gi",
            "--max-instances=1",
            "--timeout=3600",
            "--ingress=all",
            "--allow-unauthenticated",
            "--command=python",
            f"--args=^|^-c|{wrapper}",
            timeout=300,
        )
        describe = _gcloud(
            "run",
            "services",
            "describe",
            mock,
            f"--project={_PROJECT}",
            f"--region={_REGION}",
            "--format=value(status.url)",
        )
        platform_url = describe.strip().rstrip("/")
        if not platform_url.startswith("https://"):
            print(json.dumps({"phase": "source-review", "capability": "UNAVAILABLE"}))
            return 6
        deadline = time.monotonic() + 120
        artifact_sha256 = ""
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"{platform_url}/health", timeout=10
                ) as response:
                    health = json.loads(response.read().decode())
                artifact_sha256 = str(health.get("artifact_sha256") or "")
                if len(artifact_sha256) == 64:
                    break
            except (OSError, urllib.error.URLError, json.JSONDecodeError):
                time.sleep(3)
        if len(artifact_sha256) != 64:
            print(
                json.dumps(
                    {
                        "phase": "source-review",
                        "capability": "UNAVAILABLE",
                        "reason": "mock missing artifact digest",
                    }
                )
            )
            return 6
        model_key = _load_source_review_api_key()
        env_map = {
            "DITTO_PLATFORM_URL": platform_url,
            "DITTO_SOURCE_REVIEW_ID": review_id,
            "DITTO_SOURCE_REVIEW_ATTEMPT_ID": attempt_id,
            "DITTO_SOURCE_REVIEW_JOB": "1",
            "DITTO_SOURCE_REVIEW_ARTIFACT_SHA256": artifact_sha256,
            "DITTO_SOURCE_REVIEW_JOB_TOKEN": job_token,
            "SCREENER_NODE_CREDENTIAL_FILE": "/tmp/ditto-source-review/node.json",
            "SCREENER_SOURCE_REVIEW_API_KEY": model_key,
            "SCREENER_SOURCE_REVIEW_BASE_URL": "https://openrouter.ai/api/v1",
            "SCREENER_SOURCE_REVIEW_MODEL": "openai/gpt-5.6-luna",
            "SCREENER_SOURCE_REVIEW_MAX_STEPS": "40",
            "SCREENER_SOURCE_REVIEW_TIMEOUT_SECONDS": str(
                int(args.review_timeout_seconds)
            ),
            "SCREENER_STATIC_PREFLIGHT_V2_MODE": "off",
            "SCREENER_L2_REVIEW_MODE": "enforce",
            "SCREENER_L2_REVIEW_MODEL": "moonshotai/kimi-k3",
            "SCREENER_L3_REVIEW_ENABLED": "true",
            "SCREENER_L3_REVIEW_MODEL": "openai/gpt-5.6-sol",
            "SCREENER_L2_ALWAYS_ESCALATE": "true",
            "SCREENER_L2_TIMEOUT_SECONDS": "900",
            "SCREENER_L2_MAX_STEPS": "18",
            "SCREENER_L2_MAX_COMPLETION_TOKENS": "8192",
        }
        env_file = tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        )
        try:
            for key, value in env_map.items():
                env_file.write(f"{key}: {json.dumps(value)}\n")
            env_file.close()
            print(json.dumps({"phase": "create-review-job", "job": review}))
            _gcloud(
                "run",
                "jobs",
                "create",
                review,
                f"--project={_PROJECT}",
                f"--region={_REGION}",
                f"--image={args.image}",
                "--command=/app/workers/screener/.venv/bin/python",
                "--args=-m,ditto_screener.source_review_job",
                "--tasks=1",
                "--max-retries=0",
                f"--task-timeout={int(args.review_timeout_seconds) + 120}s",
                "--cpu=2",
                "--memory=4Gi",
                f"--service-account={_UNTRUSTED_SA}",
                f"--env-vars-file={env_file.name}",
                timeout=180,
            )
        finally:
            os.unlink(env_file.name)
        _gcloud(
            "run",
            "jobs",
            "execute",
            review,
            f"--project={_PROJECT}",
            f"--region={_REGION}",
            "--wait",
            timeout=int(args.review_timeout_seconds) + 180,
        )
        mock_logs = _gcloud(
            "logging",
            "read",
            (
                "resource.type=cloud_run_revision AND "
                f"resource.labels.service_name={mock}"
            ),
            f"--project={_PROJECT}",
            "--limit=80",
            "--format=value(textPayload)",
            timeout=60,
        )
        marker = next(
            (
                line.removeprefix("SOURCE_REVIEW_COMPLETE=")
                for line in mock_logs.splitlines()
                if line.startswith("SOURCE_REVIEW_COMPLETE=")
            ),
            None,
        )
        review_logs = _gcloud(
            "logging",
            "read",
            (
                "resource.type=cloud_run_job AND "
                f"resource.labels.job_name={review}"
            ),
            f"--project={_PROJECT}",
            "--limit=80",
            "--format=value(textPayload)",
            timeout=60,
        )
        print(
            json.dumps(
                {
                    "phase": "source-review",
                    "capability": "AVAILABLE" if marker else "UNAVAILABLE",
                    "observation": json.loads(marker) if marker else None,
                    "review_logs": review_logs[-4000:],
                },
                sort_keys=True,
            )
        )
        return 0 if marker else 8
    finally:
        for kind, name in (("jobs", review), ("services", mock)):
            _gcloud(
                "run",
                kind,
                "delete",
                name,
                f"--project={_PROJECT}",
                f"--region={_REGION}",
                "--quiet",
                timeout=180,
            )
            print(json.dumps({"phase": "deleted", kind: name}))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    kaniko = sub.add_parser("kaniko-probe")
    kaniko.add_argument("--starter-kit-sha", default=_STARTER_SHA)
    kaniko.add_argument("--timeout-seconds", type=float, default=1500)
    kaniko.set_defaults(handler=command_kaniko_probe)
    runtime = sub.add_parser("runtime-probe")
    runtime.add_argument("--image", required=True)
    runtime.add_argument("--timeout-seconds", type=float, default=180)
    runtime.set_defaults(handler=command_runtime_probe)
    review = sub.add_parser("source-review-probe")
    review.add_argument("--image", required=True)
    review.add_argument("--review-timeout-seconds", type=float, default=1800)
    review.set_defaults(handler=command_source_review_probe)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
