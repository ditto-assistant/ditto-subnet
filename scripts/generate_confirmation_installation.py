#!/usr/bin/env python3
"""Freeze the public, credential-free Bench v9 confirmation installation.

The generated files are release assets, not configuration supplied by a
validator operator.  They contain only public dataset identities, route/model
contracts, deterministic domain salts, and bounded budgets.  Ticket-scoped
provider capabilities are minted by Platform at claim time and therefore do
not appear here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "platform"))
sys.path.insert(0, str(ROOT / "packages" / "ditto-screening-protocol"))

from ditto.api_server.confirmation_evidence import (  # noqa: E402
    ABLATION_PROFILE_CONTRACT_VERSION,
    CAPABILITY_ORDER,
    LONGMEM_SELECTOR_REVISION_V1,
    AblationCoordinatorPolicy,
    AblationVerificationPolicy,
    CompositeVerificationPolicy,
    ConfirmationVerificationProfile,
    EmbeddingLanePolicy,
    ProviderLanePolicy,
    SyntheticBudgetPolicy,
)
from ditto_screening_protocol.confirmation_transport import (  # noqa: E402
    ConfirmationExecutionProfile,
)

DATA = (
    ROOT / "packages" / "ditto-screening-protocol" / "ditto_screening_protocol" / "data"
)
ABLATION_DATASET = DATA / "confirmation_ablation_v9_shadow.json"

# The v9 assets are frozen release data: the compose SHA-256 pin, the installed
# Platform registry, and the Go runtime factory all read these exact bytes. A
# later epoch is written as a *sibling* set, never by mutating these.
BENCH_VERSION_V9 = 9


def _asset_paths(bench_version: int) -> tuple[Path, Path, Path, Path]:
    suffix = f"v{bench_version}_shadow"
    return (
        DATA / f"confirmation_ablation_thresholds_{suffix}.json",
        DATA / f"confirmation_execution_profile_{suffix}.json",
        DATA / f"confirmation_launch_manifest_{suffix}.json",
        DATA / f"confirmation_installation_{suffix}.json",
    )


(
    THRESHOLD_MANIFEST,
    EXECUTION_PROFILE,
    LAUNCH_MANIFEST,
    INSTALLATION,
) = _asset_paths(BENCH_VERSION_V9)

LONGMEM_DATASET_SHA256 = (
    "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
)
LONGMEM_DATASET_REVISION = (
    "huggingface-98d7416c24c778c2fee6e6f3006e7a073259d48f-"
    "longmemeval-9e0b455f4ef0e2ab8f2e582289761153549043fc"
)
PROFILE_REVISION = "v9-confirmation-shadow-bounded-2026-08-25-zdr-v7"
LONGMEM_PROFILE_REVISION = "longmemeval-s-v9-shadow-48-zdr-v4"
ABLATION_PROFILE_REVISION = "dittobench-v9-ablation-shadow-6-v1"
COMPOSITE_REVISION = "v9-confirmation-composite-shadow-70-30-v1"
STARTER_MAX_AGENT_TURNS = 24

# Deep-history floors every epoch after v9 must meet, mirroring
# ``internal/longmemeval/profile.go``. The scorer validates the profile it is
# handed against exactly these numbers.
DEEP_HISTORY_FLOOR_BENCH_VERSION = 10
V10_MIN_CASES_PER_CAPABILITY = 8
V10_MIN_HISTORY_SESSIONS = 55
V10_MIN_HISTORY_BYTES = 400_000


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _file_sha256(path: Path) -> str:
    return _sha256(path.read_bytes())


def _domain_sha256(label: str) -> str:
    # Public domain separators.  Runtime projection and selection material is
    # independently derived from the signed bundle lease and never loaded from
    # these values.
    return _sha256(f"ditto-v9-confirmation-public-domain-v1:{label}".encode())


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _scale_for_deep_history(
    profile: ConfirmationVerificationProfile, bench_version: int
) -> ConfirmationVerificationProfile:
    """Lift a v9 profile onto a deep-history epoch by derivation, not invention.

    Every rail below is the v9 rail multiplied by the growth in selected cases,
    so the per-case budget policy an operator already approved for v9 is
    carried forward unchanged rather than re-guessed. The absolute cost this
    implies is materially larger than v9's, which is exactly why the resulting
    asset is a reviewable artifact and not something this script writes by
    default.
    """
    growth, remainder = divmod(
        V10_MIN_CASES_PER_CAPABILITY, profile.longmem_cases_per_capability
    )
    if remainder or growth < 1:
        raise ValueError("deep-history case count must be a multiple of the v9 count")
    return replace(
        profile,
        bench_version=bench_version,
        longmem_cases_per_capability=V10_MIN_CASES_PER_CAPABILITY,
        longmem_min_history_sessions=V10_MIN_HISTORY_SESSIONS,
        longmem_min_history_bytes=V10_MIN_HISTORY_BYTES,
        provider_lanes=tuple(
            replace(
                lane,
                max_requests=lane.max_requests * growth,
                max_prompt_tokens=lane.max_prompt_tokens * growth,
                max_completion_tokens=lane.max_completion_tokens * growth,
                max_total_tokens=lane.max_total_tokens * growth,
                max_cost_usd_micros=lane.max_cost_usd_micros * growth,
            )
            for lane in profile.provider_lanes
        ),
        embedding_lane=replace(
            profile.embedding_lane,
            max_requests=profile.embedding_lane.max_requests * growth,
            max_input_tokens=profile.embedding_lane.max_input_tokens * growth,
            max_cost_usd_micros=profile.embedding_lane.max_cost_usd_micros * growth,
        ),
    )


def _outputs(bench_version: int = BENCH_VERSION_V9) -> dict[Path, bytes]:
    threshold_path, profile_path, launch_path, installation_path = _asset_paths(
        bench_version
    )
    tag = f"v{bench_version}"
    ablation_sha = _file_sha256(ABLATION_DATASET)
    thresholds = {
        "schema_version": 1,
        "revision": "v9-confirmation-ablation-thresholds-shadow-2026-08-24",
        "mode": "shadow",
        "sample_size": 4,
        "inference_threshold_micros": 200_000,
        "embedding_threshold_micros": 200_000,
        "authority": "bounded_initial_shadow_safety_floor",
        "reward_eligible": False,
    }
    threshold_raw = _json_bytes(thresholds)

    profile = ConfirmationVerificationProfile(
        schema_version=1,
        revision=PROFILE_REVISION,
        longmem_profile_revision=LONGMEM_PROFILE_REVISION,
        longmem_profile_checksum="0" * 64,
        longmem_dataset_revision=LONGMEM_DATASET_REVISION,
        longmem_dataset_sha256=LONGMEM_DATASET_SHA256,
        longmem_selector_revision=LONGMEM_SELECTOR_REVISION_V1,
        longmem_selection_seed=17,
        longmem_cases_per_capability=8,
        longmem_seed_batch_pairs=32,
        longmem_projection_key_sha256=_domain_sha256("longmem-projection"),
        provider_lanes=(
            ProviderLanePolicy(
                lane="reader",
                provider="openrouter",
                # Routing identity, not an OpenRouter slug. The reader uses the
                # scoring LLM relay's throughput aggregate (every ZDR provider
                # except CoreWeave). Receipts record the actual OpenRouter
                # provider; this sentinel is not an equality pin.
                route_provider="throughput",
                receipt_provider="openrouter",
                profile_revision=(
                    "longmemeval-openrouter-gpt-oss-20b-throughput-zdr-shadow-v1"
                ),
                model="openai/gpt-oss-20b",
                # The public starter harness declares 24 agent turns per case.
                # Forty-eight selected cases therefore require 1,152 reader
                # requests at the frozen protocol maximum. Scale the original
                # 12-case rails by four; the per-request completion bound
                # remains 2,000 tokens.
                max_requests=1_152,
                max_prompt_tokens=14_400_000,
                max_completion_tokens=2_304_000,
                max_total_tokens=16_704_000,
                max_cost_usd_micros=6_000_000,
            ),
            ProviderLanePolicy(
                lane="judge",
                provider="openrouter",
                route_provider="azure",
                receipt_provider="Azure",
                profile_revision="longmemeval-official-gpt4o-azure-zdr-v2",
                model="openai/gpt-4o-2024-08-06",
                max_requests=48,
                max_prompt_tokens=80_000,
                max_completion_tokens=24_000,
                max_total_tokens=104_000,
                max_cost_usd_micros=4_000_000,
            ),
        ),
        embedding_lane=EmbeddingLanePolicy(
            lane="embedding",
            provider="perplexity",
            profile_revision="dittobench-v9-pplx-embed-v1-0.6b-768-v1",
            model="perplexity/pplx-embed-v1-0.6b",
            dimensions=768,
            # Live v6 Omar/lets/rb exhausted this 5,000-request rail while
            # still seeding the 48-case set (about 5,000 applied, 12 judged).
            # Scale the original 12-case 5,000 rail by four.
            max_requests=20_000,
            max_input_tokens=20_000_000,
            max_cost_usd_micros=4_000_000,
        ),
        ablation_profile_revision=ABLATION_PROFILE_REVISION,
        ablation_profile_checksum="0" * 64,
        ablation_dataset_sha256=ablation_sha,
        ablation_threshold_manifest_sha256=_sha256(threshold_raw),
        ablation_selection_key_sha256=_domain_sha256("ablation-selection"),
        ablation_projection_key_sha256=_domain_sha256("ablation-projection"),
        ablation_coordinator_policy=AblationCoordinatorPolicy(
            sample_size=4,
            max_attempts=2,
            # Floor is sample_size * 3 = 12 (one ordinary + two interventions).
            # v5 used that floor and had no retry budget. The useful max is
            # 12 * max_attempts = 24 so a retryable case can still finish.
            max_requests=24,
            request_timeout_milliseconds=90_000,
            # Go hard-max is 30 minutes. Live v5 embedding exhausted the
            # request rail, not the clock; keep the full window.
            total_timeout_milliseconds=1_800_000,
        ),
        inference_ablation=AblationVerificationPolicy(
            intervention="inference",
            contract_version=ABLATION_PROFILE_CONTRACT_VERSION,
            threshold_micros=200_000,
            budget=SyntheticBudgetPolicy(
                max_chat_requests=128,
                max_chat_input_bytes=32 * 1024 * 1024,
                max_embedding_requests=0,
                max_embedding_inputs=0,
                max_embedding_input_bytes=0,
            ),
        ),
        embedding_ablation=AblationVerificationPolicy(
            intervention="embedding",
            contract_version=ABLATION_PROFILE_CONTRACT_VERSION,
            threshold_micros=200_000,
            budget=SyntheticBudgetPolicy(
                max_chat_requests=0,
                max_chat_input_bytes=0,
                # Live v5 rb-v11-v32 exhausted 2048 applied / 2 rejected.
                # Synthetic embeddings have no provider cost; this is a
                # coordinator-time rail. 4096 is Go's hard max.
                max_embedding_requests=4_096,
                max_embedding_inputs=4_096,
                max_embedding_input_bytes=32 * 1024 * 1024,
            ),
        ),
        composite=CompositeVerificationPolicy(
            schema_version=1,
            revision=COMPOSITE_REVISION,
            formula_revision="weighted-quality-gates-v1",
            base_weight_bps=7_000,
            longmem_weight_bps=3_000,
        ),
    )
    if bench_version != BENCH_VERSION_V9:
        profile = replace(
            _scale_for_deep_history(profile, bench_version),
            revision=PROFILE_REVISION.replace("v9-", f"{tag}-", 1),
            longmem_profile_revision=LONGMEM_PROFILE_REVISION.replace(
                "-v9-", f"-{tag}-", 1
            ),
        )
    profile = replace(
        profile,
        longmem_profile_checksum=profile.longmem_checksum(),
        ablation_profile_checksum=profile.ablation_checksum(),
    )
    profile.validate()
    reader = next(lane for lane in profile.provider_lanes if lane.lane == "reader")
    selected_cases = profile.longmem_cases_per_capability * len(CAPABILITY_ORDER)
    if reader.max_requests < selected_cases * STARTER_MAX_AGENT_TURNS:
        raise ValueError("reader lane cannot cover the public starter turn contract")
    execution_payload = {**profile.payload(), "checksum": profile.checksum()}
    # Cross-check the exact public wire contract, including strict types and
    # the exact reader/judge lane population.
    ConfirmationExecutionProfile.model_validate(execution_payload)
    profile_raw = _json_bytes(execution_payload)

    launch = {
        "schema_version": 1,
        "revision": "v9-confirmation-shadow-launch-2026-08-25-zdr-v7".replace(
            "v9-", f"{tag}-", 1
        )
        if bench_version != BENCH_VERSION_V9
        else "v9-confirmation-shadow-launch-2026-08-25-zdr-v7",
        "mode": "shadow",
        "execution_profile_revision": profile.revision,
        "execution_profile_checksum": profile.checksum(),
        "longmem_dataset_sha256": LONGMEM_DATASET_SHA256,
        "ablation_dataset_sha256": ablation_sha,
        "ablation_threshold_manifest_sha256": _sha256(threshold_raw),
        "eligibility": {
            "mode": "score_threshold",
            "minimum_base_score_micros": 950_000,
        },
        "issuance_caps": {
            "daily_bundles": 1,
            # v9's caps are frozen literals with their arithmetic in a comment;
            # a deep-history epoch derives the same quantities from its own
            # frozen lanes so the cost of the larger case set is explicit.
            "daily_cost_microusd": profile.embedding_lane.max_cost_usd_micros
            + sum(lane.max_cost_usd_micros for lane in profile.provider_lanes),
            # embedding + reader + judge frozen maxima.
            "requests_per_bundle": profile.embedding_lane.max_requests
            + sum(lane.max_requests for lane in profile.provider_lanes),
            "tokens_per_bundle": profile.embedding_lane.max_input_tokens
            + sum(lane.max_total_tokens for lane in profile.provider_lanes),
        },
        "guarantees": {
            "changes_canonical_scores": False,
            "changes_rewards": False,
            "provider_authority": "platform_ticket_scoped_capabilities",
            "validator_cloud_credentials": False,
            "validator_provider_credentials": False,
        },
    }
    required_requests = profile.embedding_lane.max_requests + sum(
        lane.max_requests for lane in profile.provider_lanes
    )
    required_tokens = profile.embedding_lane.max_input_tokens + sum(
        lane.max_total_tokens for lane in profile.provider_lanes
    )
    if launch["issuance_caps"]["requests_per_bundle"] < required_requests:
        raise ValueError("bundle request cap is below frozen lane maxima")
    if launch["issuance_caps"]["tokens_per_bundle"] < required_tokens:
        raise ValueError("bundle token cap is below frozen lane maxima")
    launch_raw = _json_bytes(launch)
    installation = {
        "schema_version": 1,
        "execution_profile": execution_payload,
        "launch_manifest_sha256": _sha256(launch_raw),
        "launch_manifest_path": f"/opt/ditto/confirmation/{launch_path.name}",
        "longmem_dataset_path": ("/opt/ditto/confirmation/longmemeval_s_cleaned.json"),
        "ablation_dataset_path": (
            "/opt/ditto/confirmation/confirmation_ablation_v9_shadow.json"
        ),
        "sandbox_health_timeout_ms": 180_000,
    }
    installation_raw = _json_bytes(installation)
    return {
        threshold_path: threshold_raw,
        profile_path: profile_raw,
        launch_path: launch_raw,
        installation_path: installation_raw,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if checked-in generated assets do not have exact bytes",
    )
    parser.add_argument(
        "--bench-version",
        type=int,
        default=BENCH_VERSION_V9,
        help=(
            "confirmation epoch to freeze. The v9 assets are the shipped "
            "release data; a later epoch writes a sibling asset set whose "
            "provider rails are the v9 per-case rails scaled by the "
            "deep-history case growth, and is a cost decision to review "
            "before it is committed or installed."
        ),
    )
    args = parser.parse_args()
    outputs = _outputs(args.bench_version)
    failures: list[str] = []
    for path, raw in outputs.items():
        if args.check:
            if not path.is_file() or path.read_bytes() != raw:
                failures.append(str(path.relative_to(ROOT)))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    if failures:
        print("confirmation installation assets are out of date:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
