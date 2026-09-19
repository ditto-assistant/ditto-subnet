#!/usr/bin/env python3
"""Run one report-only Sol source-review trajectory per immutable gold case."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from ditto_screener.calibration import classification_metrics
from ditto_screener.source_review import OpenRouterSourceReviewAgent
from ditto_screening_protocol import SCREENING_POLICY_VERSION

MODEL = "openai/gpt-5.6-sol"
MAX_STEPS = 240
MAX_READ_BYTES = 16_000_000
MAX_COMPLETION_TOKENS = 32_000
TIMEOUT_SECONDS = 3_600.0


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--results-file", type=Path, required=True)
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
    os.chmod(path.parent, 0o700)
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
        response = await super()._post_completion(
            client,
            api_key,
            messages,
            timeout=timeout,
            reasoning_effort=reasoning_effort,
            tools=tools,
            tool_choice=tool_choice,
        )
        payload = response.json()
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
            self.usage["requests"] += 1
        return response


def _nonnegative_int(value: object) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else 0
    )


def _disposition(observation: object) -> str:
    ok = bool(getattr(observation, "ok", False))
    risk = getattr(observation, "risk_level", None)
    if ok and risk == "low":
        return "safe"
    if ok and risk in {"medium", "high"}:
        return "violation"
    return str(getattr(observation, "failure_disposition", "inconclusive"))


async def _main() -> None:
    args = _arguments()
    if not 1 <= args.concurrency <= 4:
        raise SystemExit("--concurrency must be between 1 and 4")
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

    root = args.artifact_root.resolve()
    semaphore = asyncio.Semaphore(args.concurrency)
    output_lock = asyncio.Lock()
    results: list[dict[str, object]] = []
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

    async def run(item: dict[str, object]) -> None:
        async with semaphore:
            expected = item.get("expected_disposition")
            if expected not in {"safe", "violation"}:
                raise ValueError("expected_disposition must be safe or violation")
            artifact_sha = str(item.get("artifact_sha256"))
            if re.fullmatch(r"[0-9a-f]{64}", artifact_sha) is None:
                raise ValueError("artifact_sha256 must be 64 lowercase hex characters")
            raw_archive = item.get("archive")
            if not isinstance(raw_archive, str) or not raw_archive:
                raise ValueError("archive must be a non-empty relative path")
            archive = (root / raw_archive).resolve()
            if not archive.is_relative_to(root):
                raise ValueError("archive must stay inside artifact-root")
            if _sha256(archive) != artifact_sha:
                raise ValueError("calibration artifact digest mismatch")

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
            actual = _disposition(observation)
            record = {
                "agent_id": item.get("agent_id"),
                "artifact_sha256": artifact_sha,
                "expected_disposition": expected,
                "actual_disposition": actual,
                "risk_level": observation.risk_level,
                "categories": list(observation.categories),
                "finding_digest": observation.finding_digest,
                "error_code": observation.error_code,
                "failure_disposition": observation.failure_disposition,
                "clearance_certified": observation.clearance_certified,
                "notes": list(observation.notes),
                "latency_ms": round((time.monotonic() - started) * 1_000),
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
                        "items": sorted(
                            results, key=lambda row: str(row.get("agent_id"))
                        ),
                    },
                )

    await asyncio.gather(*(run(item) for item in items))
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
                "reported_cost_usd": round(reported_cost, 6),
                "review": metadata,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(_main())
