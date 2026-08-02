#!/usr/bin/env python3
"""Run a private, immutable L2/L3 gold-set calibration without queue mutation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from uuid import UUID

from ditto_screener.l2_review import (
    L2_CAUSE_PROMPT_REVISION,
    L2_CAUSE_TIEBREAKER_PROMPT_REVISION,
    L2_CRITIC_PROMPT_REVISION,
    L2_DOSSIER_REVISION,
    L2_FALLBACK_MODELS,
    L2_HARNESS_REVISION,
    L2_MODEL,
    L2_PRICING_REVISION,
    L2_PROMPT_REVISION,
    L2_SAFETY_PROMPT_REVISION,
    L2_STARTER_MANIFESTS,
    L2_STATIC_HOLD_REVISION,
    L3_MODEL,
    IsolatedCodingHarness,
    KimiSolSourceReviewAgent,
    L2AuditJournal,
    L2Usage,
)
from ditto_screener.policy import SourceReviewObservation


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--analyzer-image", required=True)
    parser.add_argument("--results-file", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--audit-file", type=Path)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--analyzer-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--analyzer-cpus", type=float, default=0.5)
    parser.add_argument(
        "--artifact-sha256",
        action="append",
        dest="artifact_sha256s",
        help="run only an exact SHA-bound manifest item; may be repeated",
    )
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, raw_tmp = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


async def _main() -> None:
    args = _arguments()
    if not 1 <= args.concurrency <= 8:
        raise SystemExit("--concurrency must be between 1 and 8")
    if not 1 <= args.max_attempts <= 3:
        raise SystemExit("--max-attempts must be between 1 and 3")
    if not 30 <= args.analyzer_timeout_seconds <= 300:
        raise SystemExit("--analyzer-timeout-seconds must be between 30 and 300")
    if not 0.5 <= args.analyzer_cpus <= 2.0:
        raise SystemExit("--analyzer-cpus must be between 0.5 and 2.0")
    manifest = json.loads(args.manifest.read_text())
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise SystemExit("calibration manifest has no items")
    if args.artifact_sha256s:
        selected = set(args.artifact_sha256s)
        if any(len(value) != 64 for value in selected):
            raise SystemExit("--artifact-sha256 must be a full SHA-256 digest")
        items = [item for item in items if item.get("artifact_sha256") in selected]
        if not items:
            raise SystemExit("no manifest item matched --artifact-sha256")
    cache_dir = args.cache_dir or args.results_file.parent / "cache"
    audit_file = args.audit_file or args.results_file.parent / "audit.jsonl"
    agent = KimiSolSourceReviewAgent(
        api_key_file=str(args.api_key_file),
        base_url="https://openrouter.ai/api/v1",
        harness=IsolatedCodingHarness(
            docker_bin="docker",
            image=args.analyzer_image,
            timeout_seconds=args.analyzer_timeout_seconds,
            cpu_limit=args.analyzer_cpus,
        ),
        cache_dir=str(cache_dir),
        audit_journal=L2AuditJournal(str(audit_file), retention_days=30),
        timeout_seconds=900,
        max_steps=18,
        max_input_tokens=425_000,
        max_output_tokens=20_000,
        max_completion_tokens=2_400,
        max_cost_usd=2.00,
        cache_ttl_seconds=7 * 86_400,
        # Local IPv6 paths to OpenRouter can be unstable on some developer
        # networks. Production keeps the platform default; this read-only
        # calibration pins its disposable clients to IPv4 for repeatability.
        local_address="0.0.0.0",
    )
    metadata = {
        "models": {
            "analyst": L2_MODEL,
            "analyst_fallbacks": list(L2_FALLBACK_MODELS),
            "critic": L3_MODEL,
        },
        "revisions": {
            "analyst_prompt": L2_PROMPT_REVISION,
            "critic_prompt": L2_CRITIC_PROMPT_REVISION,
            "cause_prompt": L2_CAUSE_PROMPT_REVISION,
            "cause_tiebreaker_prompt": L2_CAUSE_TIEBREAKER_PROMPT_REVISION,
            "safety_prompt": L2_SAFETY_PROMPT_REVISION,
            "static_hold": L2_STATIC_HOLD_REVISION,
            "dossier": L2_DOSSIER_REVISION,
            "harness": L2_HARNESS_REVISION,
            "pricing": L2_PRICING_REVISION,
            "starters": [
                json.loads(path.read_text())["revision"]
                for path in L2_STARTER_MANIFESTS
            ],
        },
        "budgets": {
            "timeout_seconds": 900,
            "analyzer_timeout_seconds": args.analyzer_timeout_seconds,
            "analyzer_cpus": args.analyzer_cpus,
            "max_steps": 18,
            "max_analyzer_calls": 36,
            "max_input_tokens": 425_000,
            "max_output_tokens": 20_000,
            "max_completion_tokens": 2_400,
            "max_cost_usd": 2.00,
        },
    }
    semaphore = asyncio.Semaphore(args.concurrency)
    output_lock = asyncio.Lock()
    artifact_locks: dict[str, asyncio.Lock] = {}
    results: list[dict[str, object]] = []

    def add_usage(left: L2Usage, right: L2Usage) -> L2Usage:
        reported = (
            None
            if left.reported_cost_usd is None and right.reported_cost_usd is None
            else (left.reported_cost_usd or 0.0) + (right.reported_cost_usd or 0.0)
        )
        return L2Usage(
            input_tokens=left.input_tokens + right.input_tokens,
            output_tokens=left.output_tokens + right.output_tokens,
            cached_input_tokens=(left.cached_input_tokens + right.cached_input_tokens),
            cache_write_input_tokens=(
                left.cache_write_input_tokens + right.cache_write_input_tokens
            ),
            reasoning_tokens=left.reasoning_tokens + right.reasoning_tokens,
            estimated_cost_usd=(left.estimated_cost_usd + right.estimated_cost_usd),
            reported_cost_usd=reported,
        )

    async def run_once(item: dict[str, object]) -> None:
        async with semaphore:
            artifact_sha = str(item["artifact_sha256"])
            archive = args.artifact_root / artifact_sha / "agent.tar.gz"
            if _sha256(archive) != artifact_sha:
                raise ValueError("calibration artifact digest mismatch")
            raw_observation = item["l1_observation"]
            if not isinstance(raw_observation, dict):
                raise ValueError("calibration L1 observation is not an object")
            observation_value = dict(raw_observation)
            observation_value["categories"] = tuple(
                observation_value.get("categories", ())
            )
            usage = L2Usage()
            review_attempts = 0
            response_models: list[str] = []
            response_providers: list[str] = []
            started = time.monotonic()
            while review_attempts < args.max_attempts:
                review_attempts += 1
                deadline = asyncio.get_running_loop().time() + 900
                result = await agent.review(
                    str(archive),
                    artifact_sha256=artifact_sha,
                    attempt_id=UUID(str(item["attempt_id"])),
                    l1_observation=SourceReviewObservation(**observation_value),
                    deadline=deadline,
                )
                usage = add_usage(usage, result.usage)
                response_models.extend(result.response_models)
                response_providers.extend(result.response_providers)
                if result.observation.ok or (
                    result.observation.failure_disposition != "retryable_infra"
                ):
                    break
            observation = result.observation
            disposition = (
                "safe"
                if observation.ok and observation.risk_level == "low"
                else "violation"
                if observation.ok
                else observation.failure_disposition
            )
            record: dict[str, object] = {
                "agent_id": item["agent_id"],
                "artifact_sha256": artifact_sha,
                "expected_disposition": item["expected_disposition"],
                "expected_resolution_basis": item["expected_resolution_basis"],
                "actual_disposition": disposition,
                "actual_resolution_basis": result.resolution_basis,
                "actual_categories": list(observation.categories),
                "clearance_path": result.clearance_path,
                "dossier_complete": result.dossier_complete,
                "analyst_cache_hit": result.analyst_cache_hit,
                "critic_cache_hit": result.critic_cache_hit,
                "cache_hit": result.cache_hit,
                "review_attempts": review_attempts,
                "latency_ms": round((time.monotonic() - started) * 1_000),
                "response_models": response_models,
                "response_providers": response_providers,
                "usage": usage.__dict__,
                "error_code": observation.error_code,
                "disposition_match": disposition == item["expected_disposition"],
                "basis_match": result.resolution_basis
                == item["expected_resolution_basis"],
            }
            async with output_lock:
                results.append(record)
                _write_private_json(
                    args.results_file,
                    {
                        "revision": manifest.get("revision"),
                        "review": metadata,
                        "completed": len(results),
                        "total": len(items),
                        "items": sorted(results, key=lambda row: str(row["agent_id"])),
                    },
                )

    async def run(item: dict[str, object]) -> None:
        # Exact duplicate artifacts share an artifact/L1-bound review cache.
        # Keep duplicates outside the global concurrency semaphore while the
        # first copy is in flight so they cannot consume paid-review slots by
        # waiting on the same cache lock.
        artifact_sha = str(item["artifact_sha256"])
        lock = artifact_locks.setdefault(artifact_sha, asyncio.Lock())
        async with lock:
            await run_once(item)

    await asyncio.gather(*(run(item) for item in items))
    disposition_matches = sum(bool(item["disposition_match"]) for item in results)
    basis_matches = sum(bool(item["basis_match"]) for item in results)
    uncached = [item for item in results if not item["cache_hit"]]
    cost = 0.0
    for item in uncached:
        usage = item["usage"]
        if not isinstance(usage, dict):
            raise ValueError("calibration usage is not an object")
        cost += float(usage.get("reported_cost_usd") or 0.0)
    print(
        json.dumps(
            {
                "completed": len(results),
                "disposition_matches": disposition_matches,
                "basis_matches": basis_matches,
                "uncached_runs": len(uncached),
                "reported_cost_usd": round(cost, 6),
                "review": metadata,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(_main())
