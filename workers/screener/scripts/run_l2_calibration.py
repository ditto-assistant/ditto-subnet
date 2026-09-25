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
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from ditto_screener.calibration import classification_metrics
from ditto_screener.causal_evidence import causal_audit_fields
from ditto_screener.l2_review import (
    L2_DOSSIER_REVISION,
    L2_FALLBACK_MODELS,
    L2_HARNESS_REVISION,
    L2_MODEL,
    L2_PRICING_REVISION,
    L2_STARTER_MANIFESTS,
    L2_STATIC_HOLD_REVISION,
    L3_MODEL,
    IsolatedCodingHarness,
    L2AuditJournal,
    TerraSolSourceReviewAgent,
    l2_cause_prompt_revision,
    l2_cause_tiebreaker_prompt_revision,
    l2_critic_prompt_revision,
    l2_safety_prompt_revision,
)
from ditto_screener.policy import SourceReviewObservation
from ditto_screener.source_review import OpenRouterSourceReviewAgent
from ditto_screening_protocol import (
    SCREENING_POLICY_VERSION,
    ScoredRuntimeEvidenceLease,
)


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
    parser.add_argument("--analyzer-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--analyzer-cpus", type=float, default=0.5)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--max-steps", type=int, default=18)
    parser.add_argument("--max-input-tokens", type=int, default=425_000)
    parser.add_argument("--max-output-tokens", type=int, default=20_000)
    parser.add_argument("--max-completion-tokens", type=int, default=2_400)
    parser.add_argument("--max-cost-usd", type=float, default=2.0)
    parser.add_argument("--turn-timeout-seconds", type=float)
    parser.add_argument(
        "--sol-provider",
        choices=("azure", "azure/us", "azure/eu"),
        help="pin report-only Sol to an eligible OpenRouter Azure endpoint",
    )
    parser.add_argument(
        "--compact-review-packet",
        action="store_true",
        help="report-only SHA-bound on-demand dossier instead of full prompt dossier",
    )
    parser.add_argument(
        "--retry-provider-body-once",
        action="store_true",
        help="retry one relayed provider fault in the same model turn",
    )
    parser.add_argument(
        "--single-layer-sol",
        action="store_true",
        help="report-only GPT-6 Sol analyst with isolated tools and no L3",
    )
    parser.add_argument("--run-l1", action="store_true")
    parser.add_argument(
        "--omit-l1",
        action="store_true",
        help="single-layer Sol reviews the artifact without an L1 finding",
    )
    parser.add_argument("--l1-model", default="openai/gpt-5.6-luna")
    parser.add_argument("--l1-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--l1-max-steps", type=int, default=160)
    parser.add_argument("--l1-max-read-bytes", type=int, default=8_000_000)
    parser.add_argument("--l1-max-completion-tokens", type=int, default=8_000)
    parser.add_argument(
        "--require-label-match",
        action="store_true",
        help="exit nonzero if any disposition or resolution basis misses its label",
    )
    parser.add_argument("--scorer-capabilities-url")
    parser.add_argument("--expected-scorer-revision")
    parser.add_argument(
        "--local-cohort-packet-file",
        type=Path,
        help="simulate attempt binding from a Backroom cohort; not a signed lease",
    )
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


def _report_only_audit_cost(path: Path, *, started_at: float) -> float:
    """Count billed turns even when a later provider fault loses final usage."""
    if not path.exists():
        return 0.0
    cost = 0.0
    for line in path.read_text().splitlines():
        event = json.loads(line)
        if (
            event.get("event_type") == "report_only_turn_usage"
            and isinstance(event.get("recorded_at"), (int, float))
            and event["recorded_at"] >= started_at
        ):
            value = event.get("reported_cost_usd")
            if isinstance(value, (int, float)) and value >= 0:
                cost += float(value)
    return cost


async def _main() -> None:
    args = _arguments()
    if not 1 <= args.concurrency <= 8:
        raise SystemExit("--concurrency must be between 1 and 8")
    if not 30 <= args.analyzer_timeout_seconds <= 300:
        raise SystemExit("--analyzer-timeout-seconds must be between 30 and 300")
    if not 0.5 <= args.analyzer_cpus <= 2.0:
        raise SystemExit("--analyzer-cpus must be between 0.5 and 2.0")
    if not 30 <= args.timeout_seconds <= 1_800:
        raise SystemExit("--timeout-seconds must be between 30 and 1800")
    if not 1 <= args.max_steps <= 256:
        raise SystemExit("--max-steps must be between 1 and 256")
    if not 1 <= args.max_input_tokens <= 5_000_000:
        raise SystemExit("--max-input-tokens must be between 1 and 5000000")
    if not 1 <= args.max_output_tokens <= 1_000_000:
        raise SystemExit("--max-output-tokens must be between 1 and 1000000")
    if not 1 <= args.max_completion_tokens <= 16_000:
        raise SystemExit("--max-completion-tokens must be between 1 and 16000")
    if not 0 < args.max_cost_usd <= 25:
        raise SystemExit("--max-cost-usd must be between 0 and 25")
    if args.turn_timeout_seconds is not None and not (
        30 <= args.turn_timeout_seconds <= 600
    ):
        raise SystemExit("--turn-timeout-seconds must be between 30 and 600")
    if args.single_layer_sol and not args.require_label_match:
        raise SystemExit("--single-layer-sol requires --require-label-match")
    if args.omit_l1 and (not args.single_layer_sol or args.run_l1):
        raise SystemExit("--omit-l1 requires --single-layer-sol and excludes --run-l1")
    if args.retry_provider_body_once and not args.single_layer_sol:
        raise SystemExit("--retry-provider-body-once is report-only Sol mode")
    if args.sol_provider and not args.single_layer_sol:
        raise SystemExit("--sol-provider is report-only Sol mode")
    if args.compact_review_packet and not args.single_layer_sol:
        raise SystemExit("--compact-review-packet is report-only Sol mode")
    if not 30 <= args.l1_timeout_seconds <= 600:
        raise SystemExit("--l1-timeout-seconds must be between 30 and 600")
    if not 1 <= args.l1_max_steps <= 160:
        raise SystemExit("--l1-max-steps must be between 1 and 160")
    if not 1 <= args.l1_max_read_bytes <= 8_000_000:
        raise SystemExit("--l1-max-read-bytes must be between 1 and 8000000")
    if not 1 <= args.l1_max_completion_tokens <= 8_000:
        raise SystemExit("--l1-max-completion-tokens must be between 1 and 8000")
    if bool(args.scorer_capabilities_url) != bool(args.expected_scorer_revision):
        raise SystemExit("scorer capabilities URL and revision must be set together")
    if args.local_cohort_packet_file and args.scorer_capabilities_url:
        raise SystemExit("choose either a cohort packet or scorer capabilities URL")
    cohort: dict[str, object] | None = None
    if args.local_cohort_packet_file:
        raw_cohort = json.loads(args.local_cohort_packet_file.read_text())
        if not isinstance(raw_cohort, dict) or raw_cohort.get("bench_version") != 13:
            raise SystemExit("local cohort packet must be a V13 Backroom response")
        if not isinstance(raw_cohort.get("packet"), dict):
            raise SystemExit("local cohort packet is missing its immutable packet")
        if not isinstance(raw_cohort.get("hotkeys"), list) or not raw_cohort["hotkeys"]:
            raise SystemExit("local cohort packet is missing validator hotkeys")
        cohort = raw_cohort
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
    run_started_at = time.time()
    analyst_model = "openai/gpt-6-sol" if args.single_layer_sol else L2_MODEL
    agent = TerraSolSourceReviewAgent(
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
        timeout_seconds=args.timeout_seconds,
        max_steps=args.max_steps,
        max_input_tokens=args.max_input_tokens,
        max_output_tokens=args.max_output_tokens,
        max_completion_tokens=args.max_completion_tokens,
        max_cost_usd=args.max_cost_usd,
        cache_ttl_seconds=7 * 86_400,
        max_completion_request_seconds=args.turn_timeout_seconds,
        independent_analyst=args.omit_l1,
        terminal_verdict_required=args.single_layer_sol,
        retry_provider_body_fault_once=args.retry_provider_body_once,
        analyst_provider=args.sol_provider,
        compact_review_packet=args.compact_review_packet,
        model=analyst_model,
        fallback_models=() if args.single_layer_sol else L2_FALLBACK_MODELS,
        l3_enabled=not args.single_layer_sol,
        scorer_capabilities_url=args.scorer_capabilities_url,
        expected_scorer_revision=args.expected_scorer_revision,
        # Local IPv6 paths to OpenRouter can be unstable on some developer
        # networks. Production keeps the platform default; this read-only
        # calibration pins its disposable clients to IPv4 for repeatability.
        local_address="0.0.0.0",
    )
    l1_agent = (
        OpenRouterSourceReviewAgent(
            api_key_file=str(args.api_key_file),
            model=args.l1_model,
            base_url="https://openrouter.ai/api/v1",
            timeout_seconds=args.l1_timeout_seconds,
            max_steps=args.l1_max_steps,
            max_read_bytes=args.l1_max_read_bytes,
            max_completion_tokens=args.l1_max_completion_tokens,
            reasoning_effort="high",
            static_preflight_v2_mode="off",
            concern_hold_count=3,
            clear_min_notes=3,
        )
        if args.run_l1
        else None
    )
    metadata = {
        "models": {
            "analyst": analyst_model,
            "analyst_fallbacks": (
                [] if args.single_layer_sol else list(L2_FALLBACK_MODELS)
            ),
            "critic": None if args.single_layer_sol else L3_MODEL,
        },
        "review_mode": (
            "report_only_single_layer_sol"
            if args.single_layer_sol
            else "production_multilayer"
        ),
        "terminal_verdict_required": args.single_layer_sol,
        "retry_provider_body_fault_once": args.retry_provider_body_once,
        "sol_provider": args.sol_provider,
        "compact_review_packet": args.compact_review_packet,
        "revisions": {
            "analyst_prompt": agent._analyst_prompt_revision(SCREENING_POLICY_VERSION),
            "critic_prompt": l2_critic_prompt_revision(SCREENING_POLICY_VERSION),
            "cause_prompt": l2_cause_prompt_revision(SCREENING_POLICY_VERSION),
            "cause_tiebreaker_prompt": l2_cause_tiebreaker_prompt_revision(
                SCREENING_POLICY_VERSION
            ),
            "safety_prompt": l2_safety_prompt_revision(SCREENING_POLICY_VERSION),
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
            "timeout_seconds": args.timeout_seconds,
            "analyzer_timeout_seconds": args.analyzer_timeout_seconds,
            "analyzer_cpus": args.analyzer_cpus,
            "max_steps": args.max_steps,
            "max_analyzer_calls": 2 * args.max_steps,
            "max_input_tokens": args.max_input_tokens,
            "max_output_tokens": args.max_output_tokens,
            "max_completion_tokens": args.max_completion_tokens,
            "max_cost_usd": args.max_cost_usd,
            "turn_timeout_seconds": args.turn_timeout_seconds or 45.0,
        },
        "runtime_evidence_origin": (
            "local_cohort_packet_simulation"
            if cohort
            else "scorer_capabilities"
            if args.scorer_capabilities_url
            else "absent"
        ),
        "l1_mode": (
            "omitted"
            if args.omit_l1
            else "fresh_local_review"
            if l1_agent
            else "archived_observation"
        ),
    }
    semaphore = asyncio.Semaphore(args.concurrency)
    output_lock = asyncio.Lock()
    artifact_locks: dict[str, asyncio.Lock] = {}
    results: list[dict[str, object]] = []

    async def run_once(item: dict[str, object]) -> None:
        async with semaphore:
            artifact_sha = str(item["artifact_sha256"])
            archive = args.artifact_root / artifact_sha / "agent.tar.gz"
            if _sha256(archive) != artifact_sha:
                raise ValueError("calibration artifact digest mismatch")
            started = time.monotonic()
            deadline = asyncio.get_running_loop().time() + args.timeout_seconds
            if l1_agent:
                l1_observation = await l1_agent.review(
                    str(archive),
                    artifact_sha256=artifact_sha,
                    deadline=min(
                        deadline,
                        asyncio.get_running_loop().time() + args.l1_timeout_seconds,
                    ),
                )
                if not l1_observation.ok:
                    raise ValueError(
                        "local L1 did not produce a complete observation: "
                        f"{l1_observation.error_code}"
                    )
                _write_private_json(
                    args.results_file.parent
                    / "l1-checkpoints"
                    / f"{artifact_sha}.json",
                    {
                        "artifact_sha256": artifact_sha,
                        "model": args.l1_model,
                        "observation": asdict(l1_observation),
                    },
                )
            elif args.omit_l1:
                l1_observation = SourceReviewObservation(
                    ok=True,
                    risk_level=None,
                    finding_digest=None,
                    categories=(),
                )
            else:
                raw_observation = item.get("l1_observation")
                if not isinstance(raw_observation, dict):
                    raise ValueError("calibration L1 observation is not an object")
                observation_value = dict(raw_observation)
                observation_value["categories"] = tuple(
                    observation_value.get("categories", ())
                )
                l1_observation = SourceReviewObservation(**observation_value)
            local_lease = None
            if cohort:
                packet = cohort["packet"]
                assert isinstance(packet, dict)
                hotkeys = cohort["hotkeys"]
                assert isinstance(hotkeys, list)
                local_lease = ScoredRuntimeEvidenceLease.model_validate(
                    {
                        "attempt_id": UUID(str(item["attempt_id"])),
                        "artifact_sha256": artifact_sha,
                        "policy_version": 13,
                        "bench_version": 13,
                        "scorer_source_revision": packet["source_revision"],
                        "release_descriptor_digest": packet[
                            "release_descriptor_digest"
                        ],
                        "scorer_image_digest": packet["scorer_image_digest"],
                        "scorer_env_sha256": packet["scorer_env_sha256"],
                        "injected_keys": packet["injected_keys"],
                        "validator_count": len(hotkeys),
                        "observed_at": int(time.time()),
                    }
                )
            result = await agent.review(
                str(archive),
                artifact_sha256=artifact_sha,
                attempt_id=UUID(str(item["attempt_id"])),
                l1_observation=l1_observation,
                deadline=deadline,
                scored_runtime_evidence=local_lease,
            )
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
                "review_attempts": 1,
                "latency_ms": round((time.monotonic() - started) * 1_000),
                "response_models": list(result.response_models),
                "response_providers": list(result.response_providers),
                "usage": result.usage.__dict__,
                "error_code": observation.error_code,
                "l1_risk_level": l1_observation.risk_level,
                "l1_categories": list(l1_observation.categories),
                "l1_finding_digest": l1_observation.finding_digest,
                "l1_finding": l1_observation.finding,
                "l1_review_audit": l1_observation.review_audit,
                "l1_notes_count": len(l1_observation.notes),
                "finding_digest": observation.finding_digest,
                "review_audit": observation.review_audit,
                **causal_audit_fields(observation.finding),
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
                        "classification": classification_metrics(results),
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
        cost += float(
            usage.get("reported_cost_usd") or usage.get("estimated_cost_usd") or 0.0
        )
    terminal_decisions = sum(
        item["actual_disposition"] in {"safe", "violation"} for item in results
    )
    if args.single_layer_sol:
        cost = max(cost, _report_only_audit_cost(audit_file, started_at=run_started_at))
    _write_private_json(
        args.results_file,
        {
            "revision": manifest.get("revision"),
            "review": metadata,
            "completed": len(results),
            "total": len(items),
            "classification": classification_metrics(results),
            "reported_cost_usd": round(cost, 6),
            "terminal_decisions": terminal_decisions,
            "no_decision_cases": len(results) - terminal_decisions,
            "items": sorted(results, key=lambda row: str(row["agent_id"])),
        },
    )
    print(
        json.dumps(
            {
                "completed": len(results),
                "disposition_matches": disposition_matches,
                "basis_matches": basis_matches,
                "classification": classification_metrics(results),
                "uncached_runs": len(uncached),
                "reported_cost_usd": round(cost, 6),
                "terminal_decisions": terminal_decisions,
                "no_decision_cases": len(results) - terminal_decisions,
                "review": metadata,
            },
            sort_keys=True,
        )
    )
    if args.require_label_match and (
        disposition_matches != len(results) or basis_matches != len(results)
    ):
        raise SystemExit("calibration did not match all expected labels")


if __name__ == "__main__":
    asyncio.run(_main())
