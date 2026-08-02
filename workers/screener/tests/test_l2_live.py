"""Opt-in live L2 resolver + SOL L3 acceptance against the canonical starter."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import subprocess
import tarfile
import time
from pathlib import Path
from uuid import UUID

import pytest

from ditto_screener.l2_review import (
    L2_FALLBACK_MODELS,
    IsolatedCodingHarness,
    L2AuditJournal,
    SolL2SourceReviewAgent,
)
from ditto_screener.policy import SourceReviewObservation
from ditto_screening_protocol import SourceReviewEvidenceItem, SourceReviewFinding

ROOT = Path(__file__).resolve().parents[1]
ATTEMPT = UUID("d7a5d43b-0870-41e7-b4b8-e8d45293a337")
V5_STARTER_SHA = "ae3dd10975cd7c6a7f64cbcb956db0c85a2b2797"
V6_STARTER_SHA = "b11934ee7687e5dfc0569d9e75c619676b399058"


def _tracked_starter_archive(starter: Path, output: Path) -> str:
    names = (
        subprocess.check_output(["git", "ls-files", "-z"], cwd=starter)
        .decode()
        .split("\0")
    )
    with tarfile.open(output, "w:gz") as archive:
        for name in names:
            if not name:
                continue
            raw = (starter / name).read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(raw))
    return hashlib.sha256(output.read_bytes()).hexdigest()


@pytest.mark.live
async def test_live_l2_analyst_and_sol_critic_clear_canonical_starter(
    tmp_path: Path,
) -> None:
    starter_raw = os.environ.get("DITTO_STARTER_KIT_DIR")
    key_file = os.environ.get("DITTO_L2_LIVE_OPENROUTER_KEY_FILE")
    if not starter_raw or not key_file:
        pytest.skip(
            "DITTO_STARTER_KIT_DIR and DITTO_L2_LIVE_OPENROUTER_KEY_FILE required"
        )
    analyst_model = os.environ.get("DITTO_L2_LIVE_ANALYST_MODEL", "moonshotai/kimi-k3")
    assert analyst_model in {"moonshotai/kimi-k3", "z-ai/glm-5.2"}
    fallback_models = (
        L2_FALLBACK_MODELS
        if analyst_model == "moonshotai/kimi-k3"
        else ("openai/gpt-5.6-sol",)
    )
    timeout_seconds = float(os.environ.get("DITTO_L2_LIVE_TIMEOUT_SECONDS", "600"))
    max_input_tokens = int(os.environ.get("DITTO_L2_LIVE_MAX_INPUT_TOKENS", "425000"))
    max_output_tokens = int(os.environ.get("DITTO_L2_LIVE_MAX_OUTPUT_TOKENS", "20000"))
    max_cost_usd = float(os.environ.get("DITTO_L2_LIVE_MAX_COST_USD", "2.00"))
    starter = Path(starter_raw).resolve()
    expected_starter_sha = os.environ.get("DITTO_L2_LIVE_STARTER_SHA", V6_STARTER_SHA)
    assert expected_starter_sha in {V5_STARTER_SHA, V6_STARTER_SHA}
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=starter)
        .decode()
        .strip()
        == expected_starter_sha
    )
    image = "ditto-screener-l2-analyzer:live"
    build = await asyncio.create_subprocess_exec(
        "docker",
        "build",
        "-f",
        str(ROOT / "deploy/l2-analyzer.Dockerfile"),
        "-t",
        image,
        str(ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await build.communicate()
    assert build.returncode == 0, output.decode(errors="replace")[-4_000:]

    archive = tmp_path / "canonical-starter.tar.gz"
    artifact_sha = _tracked_starter_archive(starter, archive)
    finding = SourceReviewFinding(
        artifact_sha256=artifact_sha,
        prompt_revision="live-synthetic-l1-lead-v1",
        risk_level="medium",
        confidence=0.5,
        categories=["benchmark_emulation"],
        evidence=[
            SourceReviewEvidenceItem(
                path="src/bin/dittobench-miner.rs",
                line=1,
                category="benchmark_emulation",
            )
        ],
        summary="Synthetic ambiguous routing lead for live acceptance.",
    )
    l1 = SourceReviewObservation(
        ok=True,
        risk_level="medium",
        finding_digest=finding.canonical_digest(),
        categories=("benchmark_emulation",),
        finding=finding.model_dump(mode="json"),
    )
    audit = tmp_path / "audit.jsonl"
    agent = SolL2SourceReviewAgent(
        api_key_file=key_file,
        base_url="https://openrouter.ai/api/v1",
        harness=IsolatedCodingHarness(docker_bin="docker", image=image),
        cache_dir=str(tmp_path / "cache"),
        audit_journal=L2AuditJournal(str(audit), retention_days=1),
        timeout_seconds=timeout_seconds,
        max_steps=12,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_completion_tokens=2_400,
        max_cost_usd=max_cost_usd,
        cache_ttl_seconds=3_600,
        model=analyst_model,
        fallback_models=fallback_models,
    )
    started = time.monotonic()
    attempts = 0
    while attempts < 3:
        attempts += 1
        result = await agent.review(
            str(archive),
            artifact_sha256=artifact_sha,
            attempt_id=ATTEMPT,
            l1_observation=l1,
            deadline=None,
        )
        if result.observation.ok or (
            result.observation.failure_disposition != "retryable_infra"
        ):
            break
    elapsed = time.monotonic() - started

    assert result.observation.ok
    assert result.observation.risk_level == "low"
    assert result.critic_disposition == "confirm_safe"
    assert result.response_models
    assert result.response_models[0].startswith(analyst_model)
    assert result.response_models[-1].startswith("openai/gpt-5.6-sol")
    assert result.response_providers[0]
    assert result.response_providers[-1] in {"Azure", "OpenAI"}
    records = [json.loads(line) for line in audit.read_text().splitlines()]
    record = records[-1]
    assert record["critic_disposition"] == "confirm_safe"
    assert record["analyst_model"] == analyst_model
    assert record["analyst_fallback_models"] == list(fallback_models)
    assert "summary" not in record
    print(
        json.dumps(
            {
                "disposition": "safe",
                "critic": result.critic_disposition,
                "models": list(result.response_models),
                "providers": list(result.response_providers),
                "analyst_tool_calls": len(result.analyst_tools),
                "critic_tool_calls": len(result.critic_tools),
                "input_tokens": result.usage.input_tokens,
                "cached_input_tokens": result.usage.cached_input_tokens,
                "cache_write_input_tokens": result.usage.cache_write_input_tokens,
                "output_tokens": result.usage.output_tokens,
                "reasoning_tokens": result.usage.reasoning_tokens,
                "reported_cost_usd": result.usage.reported_cost_usd,
                "elapsed_seconds": round(elapsed, 3),
                "attempts": attempts,
            },
            sort_keys=True,
        )
    )
