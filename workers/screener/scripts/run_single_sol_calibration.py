#!/usr/bin/env python3
"""Run one report-only Sol source-review trajectory per immutable gold case."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from ditto_screener.calibration import (
    classification_metrics,
    disposition_metrics,
    review_disposition,
)
from ditto_screener.policy import SourceReviewObservation
from ditto_screener.source_review import OpenRouterSourceReviewAgent, _prompt_revision
from ditto_screening_protocol import SCREENING_POLICY_VERSION
from ditto_screening_protocol.models import SourceReviewFinding

MODEL = "openai/gpt-5.6-sol"
MAX_STEPS = 240
MAX_READ_BYTES = 16_000_000
MAX_COMPLETION_TOKENS = 32_000
TIMEOUT_SECONDS = 3_600.0
SHA_RE = re.compile(r"[0-9a-f]{64}")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--results-file", type=Path, required=True)
    parser.add_argument(
        "--handoff-file",
        type=Path,
        help="write a private exact-attempt Sol handoff for paired L4 replay",
    )
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--policy-version", type=int, default=SCREENING_POLICY_VERSION)
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
    if path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
        raise ValueError("private output directory must be mode 0700")
    if path.is_symlink():
        raise ValueError("private output file must not be a symlink")
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class MeteredSingleSolReviewer(OpenRouterSourceReviewAgent):
    """Capture response metadata without retaining source or model transcripts."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.usage = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "reported_cost_usd": 0.0,
            "requests": 0,
            "responses": 0,
            "request_failures": 0,
            "unmetered_responses": 0,
        }
        self.response_models: set[str] = set()
        self.response_providers: set[str] = set()

    async def _post_completion(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        messages: list[dict[str, object]],
        *,
        timeout: float | None = None,
        reasoning_effort: str,
        tools: Sequence[Mapping[str, object]] | None = None,
        tool_choice: str = "auto",
    ) -> httpx.Response:
        self.usage["requests"] += 1
        try:
            response = await super()._post_completion(
                client,
                api_key,
                messages,
                timeout=timeout,
                reasoning_effort=reasoning_effort,
                tools=tools,
                tool_choice=tool_choice,
            )
        except Exception:
            self.usage["request_failures"] += 1
            raise
        self.usage["responses"] += 1
        try:
            payload = response.json()
        except ValueError:
            self.usage["unmetered_responses"] += 1
            raise
        cost_reported = False
        if isinstance(payload, dict):
            model = payload.get("model")
            provider = payload.get("provider")
            if isinstance(model, str):
                self.response_models.add(model)
            if isinstance(provider, str):
                self.response_providers.add(provider)
            usage = payload.get("usage")
            if isinstance(usage, dict):
                self.usage["input_tokens"] += _nonnegative_int(
                    usage.get("prompt_tokens")
                )
                self.usage["output_tokens"] += _nonnegative_int(
                    usage.get("completion_tokens")
                )
                prompt_details = usage.get("prompt_tokens_details")
                if isinstance(prompt_details, dict):
                    self.usage["cached_input_tokens"] += _nonnegative_int(
                        prompt_details.get("cached_tokens")
                    )
                completion_details = usage.get("completion_tokens_details")
                if isinstance(completion_details, dict):
                    self.usage["reasoning_tokens"] += _nonnegative_int(
                        completion_details.get("reasoning_tokens")
                    )
                cost = usage.get("cost")
                if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                    self.usage["reported_cost_usd"] += max(0.0, float(cost))
                    cost_reported = True
        if not cost_reported:
            self.usage["unmetered_responses"] += 1
        return response


def _nonnegative_int(value: object) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else 0
    )


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _handoff_identity(item: object, policy_version: int) -> dict[str, object]:
    """Require the exact identifiers consumed by the paired L4 manifest.

    This preflight runs over every selected case before any paid model call.
    An artifact SHA alone is not enough to identify a screening attempt.
    """
    if not isinstance(item, dict):
        raise ValueError("handoff case must be an object")
    identity: dict[str, object] = {"policy_version": policy_version}
    for field in ("agent_id", "attempt_id"):
        value = item.get(field)
        try:
            parsed = UUID(str(value))
        except ValueError as error:
            raise ValueError(f"handoff {field} must be a UUID") from error
        if value != str(parsed):
            raise ValueError(f"handoff {field} must be canonical lowercase UUID")
        identity[field] = value
    for field in ("artifact_sha256", "manifest_digest"):
        value = item.get(field)
        if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
            raise ValueError(f"handoff {field} must be a full lowercase SHA-256")
        identity[field] = value
    revision = item.get("review_settings_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("handoff review_settings_revision must be positive")
    identity["review_settings_revision"] = revision
    return identity


def _sol_handoff(
    identity: Mapping[str, object],
    observation: SourceReviewObservation,
    reviewer: MeteredSingleSolReviewer,
    latency_ms: int,
) -> dict[str, object]:
    """Export only the bounded ledger and canonical finding, never source files.

    The output is private and report-only. Missing/ambiguous metering remains
    null, which makes the downstream two-layer replay incomplete, not free.
    """
    notes = [dict(note) for note in observation.notes]
    if len(notes) > 48:
        raise ValueError("handoff notes exceed the bounded reviewer ledger")
    finding = None
    if observation.finding is not None:
        parsed = SourceReviewFinding.model_validate(observation.finding)
        if parsed.artifact_sha256 != identity["artifact_sha256"]:
            raise ValueError("handoff finding artifact differs from exact case")
        if parsed.prompt_revision != _prompt_revision(int(identity["policy_version"])):
            raise ValueError("handoff finding prompt revision differs from reviewer")
        if parsed.canonical_digest() != observation.finding_digest:
            raise ValueError("handoff finding digest mismatch")
        finding = parsed.model_dump(mode="json", exclude_none=True)
    elif observation.finding_digest is not None:
        raise ValueError("handoff finding digest without canonical finding")

    usage = reviewer.usage
    reported_cost = usage.get("reported_cost_usd")
    metered = (
        reviewer.response_models == {MODEL}
        and usage.get("responses", 0) > 0
        and usage.get("requests") == usage.get("responses")
        and usage.get("request_failures") == 0
        and usage.get("unmetered_responses") == 0
        and isinstance(reported_cost, (int, float))
        and not isinstance(reported_cost, bool)
        and math.isfinite(reported_cost)
        and reported_cost >= 0
    )
    return {
        **identity,
        "model": MODEL,
        "prompt_revision": _prompt_revision(int(identity["policy_version"])),
        "notes": notes,
        "notes_payload_sha256": _canonical_json_sha256(notes),
        "finding": finding,
        "finding_digest": observation.finding_digest,
        "error_code": observation.error_code,
        "latency_ms": latency_ms,
        "reported_cost_usd": reported_cost if metered else None,
        "compactions": reviewer.compaction_count,
    }


def _sanitized_invariant_assessment(finding: object) -> dict[str, object] | None:
    """Retain only bounded policy decisions and source locations, never source text."""

    if finding is None:
        return None
    parsed = SourceReviewFinding.model_validate(finding)
    assessment = parsed.invariant_assessment
    if assessment is None:
        return None
    return {
        "schema_version": assessment.schema_version,
        "decisions": [
            {
                "invariant": decision.invariant.value,
                "disposition": decision.disposition.value,
                "pass_clause": (
                    decision.pass_clause.value if decision.pass_clause else None
                ),
                "summary_sha256": hashlib.sha256(decision.summary.encode()).hexdigest(),
                "evidence": [
                    {
                        "path": parsed.evidence[index].path,
                        "line": parsed.evidence[index].line,
                        "category": parsed.evidence[index].category,
                    }
                    for index in decision.evidence_indices
                ],
            }
            for decision in assessment.decisions
        ],
    }


async def _main() -> None:
    args = _arguments()
    if not 1 <= args.concurrency <= 4:
        raise SystemExit("--concurrency must be between 1 and 4")
    manifest = json.loads(args.manifest.read_text())
    if not isinstance(manifest, dict):
        raise SystemExit("calibration manifest must be an object")
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

    root = args.artifact_root.resolve()
    if args.handoff_file is not None and args.handoff_file.resolve().is_relative_to(
        root
    ):
        raise SystemExit("handoff file must be outside artifact-root")
    handoff_identities: dict[tuple[str, str], dict[str, object]] = {}
    if args.handoff_file is not None:
        if args.policy_version != 13:
            raise SystemExit("paired L4 handoff requires policy version 13")
        if not isinstance(manifest.get("revision"), str) or not manifest["revision"]:
            raise SystemExit("paired L4 handoff needs a manifest revision")
        destinations = {args.results_file.resolve(), args.manifest.resolve()}
        if args.handoff_file.resolve() in destinations:
            raise SystemExit("handoff file must differ from input and results files")
        if args.handoff_file.exists() or args.handoff_file.is_symlink():
            raise SystemExit("handoff output already exists; choose a fresh path")

    prepared: list[tuple[dict[str, object], Path, str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("calibration case must be an object")
        if args.handoff_file is not None:
            identity = _handoff_identity(item, args.policy_version)
            key = (str(identity["agent_id"]), str(identity["attempt_id"]))
            if key in handoff_identities:
                raise SystemExit("duplicate exact agent/attempt in handoff manifest")
            handoff_identities[key] = identity
        expected = item.get("expected_disposition")
        if not isinstance(expected, str) or expected not in {"safe", "violation"}:
            raise ValueError("expected_disposition must be safe or violation")
        artifact_sha = item.get("artifact_sha256")
        if not isinstance(artifact_sha, str) or SHA_RE.fullmatch(artifact_sha) is None:
            raise ValueError("artifact_sha256 must be 64 lowercase hex characters")
        raw_archive = item.get("archive")
        if not isinstance(raw_archive, str) or not raw_archive:
            raise ValueError("archive must be a non-empty relative path")
        archive = (root / raw_archive).resolve()
        if not archive.is_relative_to(root):
            raise ValueError("archive must stay inside artifact-root")
        if _sha256(archive) != artifact_sha:
            raise ValueError("calibration artifact digest mismatch")
        prepared.append((item, archive, artifact_sha, expected))

    semaphore = asyncio.Semaphore(args.concurrency)
    output_lock = asyncio.Lock()
    results: list[dict[str, object]] = []
    handoffs: list[dict[str, object]] = []
    metadata = {
        "model": MODEL,
        "policy_version": args.policy_version,
        "reasoning_effort": "high",
        "compaction_checkpoint_version": 1,
        "budgets": {
            "timeout_seconds": TIMEOUT_SECONDS,
            "max_steps": MAX_STEPS,
            "max_read_bytes": MAX_READ_BYTES,
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
        },
    }

    async def run(
        item: dict[str, object], archive: Path, artifact_sha: str, expected: str
    ) -> None:
        async with semaphore:
            reviewer = MeteredSingleSolReviewer(
                api_key_file=str(args.api_key_file),
                model=MODEL,
                base_url="https://openrouter.ai/api/v1",
                timeout_seconds=TIMEOUT_SECONDS,
                max_steps=MAX_STEPS,
                max_read_bytes=MAX_READ_BYTES,
                max_completion_tokens=MAX_COMPLETION_TOKENS,
                reasoning_effort="high",
                concern_hold_count=3,
                clear_min_notes=5,
            )
            started = time.monotonic()
            observation = await reviewer.review(
                str(archive),
                artifact_sha256=artifact_sha,
                deadline=asyncio.get_running_loop().time() + TIMEOUT_SECONDS,
                policy_version=args.policy_version,
            )
            actual = review_disposition(observation)
            latency_ms = round((time.monotonic() - started) * 1_000)
            handoff = None
            if args.handoff_file is not None:
                key = (str(item["agent_id"]), str(item["attempt_id"]))
                identity = handoff_identities[key]
                handoff = {
                    **identity,
                    "sol_investigator": _sol_handoff(
                        identity, observation, reviewer, latency_ms
                    ),
                }
            record = {
                "agent_id": item.get("agent_id"),
                "artifact_sha256": artifact_sha,
                "expected_disposition": expected,
                "actual_disposition": actual,
                "risk_level": observation.risk_level,
                "categories": list(observation.categories),
                "finding_digest": observation.finding_digest,
                "invariant_assessment": _sanitized_invariant_assessment(
                    observation.finding
                ),
                "error_code": observation.error_code,
                "failure_disposition": observation.failure_disposition,
                "clearance_certified": observation.clearance_certified,
                "notes": list(observation.notes),
                "latency_ms": latency_ms,
                "compactions": reviewer.compaction_count,
                "usage": reviewer.usage,
                "response_models": sorted(reviewer.response_models),
                "response_providers": sorted(reviewer.response_providers),
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
                        "dispositions": disposition_metrics(results),
                        "items": sorted(
                            results, key=lambda row: str(row.get("agent_id"))
                        ),
                    },
                )
                if handoff is not None:
                    handoffs.append(handoff)
                    _write_private_json(
                        args.handoff_file,
                        {
                            "schema_version": 1,
                            "revision": manifest["revision"],
                            "policy_version": args.policy_version,
                            "items": sorted(
                                handoffs,
                                key=lambda row: (
                                    str(row["agent_id"]),
                                    str(row["attempt_id"]),
                                ),
                            ),
                        },
                    )

    await asyncio.gather(*(run(*case) for case in prepared))
    reported_cost = 0.0
    for row in results:
        usage = row.get("usage")
        if isinstance(usage, dict):
            value = usage.get("reported_cost_usd")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                reported_cost += max(0.0, float(value))
    print(
        json.dumps(
            {
                "completed": len(results),
                "classification": classification_metrics(results),
                "dispositions": disposition_metrics(results),
                "reported_cost_usd": round(reported_cost, 6),
                "review": metadata,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(_main())
