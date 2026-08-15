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
THRESHOLD_MANIFEST = DATA / "confirmation_ablation_thresholds_v9_shadow.json"
EXECUTION_PROFILE = DATA / "confirmation_execution_profile_v9_shadow.json"
LAUNCH_MANIFEST = DATA / "confirmation_launch_manifest_v9_shadow.json"
INSTALLATION = DATA / "confirmation_installation_v9_shadow.json"

LONGMEM_DATASET_SHA256 = (
    "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
)
LONGMEM_DATASET_REVISION = (
    "huggingface-98d7416c24c778c2fee6e6f3006e7a073259d48f-"
    "longmemeval-9e0b455f4ef0e2ab8f2e582289761153549043fc"
)
PROFILE_REVISION = "v9-confirmation-shadow-bounded-2026-08-15-zdr-v3"
LONGMEM_PROFILE_REVISION = "longmemeval-s-v9-shadow-12-zdr-v3"
ABLATION_PROFILE_REVISION = "dittobench-v9-ablation-shadow-4-v1"
COMPOSITE_REVISION = "v9-confirmation-composite-shadow-70-30-v1"
STARTER_MAX_AGENT_TURNS = 24


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


def _outputs() -> dict[Path, bytes]:
    ablation_sha = _file_sha256(ABLATION_DATASET)
    thresholds = {
        "schema_version": 1,
        "revision": "v9-confirmation-ablation-thresholds-shadow-2026-08-13",
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
        longmem_cases_per_capability=2,
        longmem_seed_batch_pairs=32,
        longmem_projection_key_sha256=_domain_sha256("longmem-projection"),
        provider_lanes=(
            ProviderLanePolicy(
                lane="reader",
                provider="openrouter",
                route_provider="deepinfra",
                receipt_provider="DeepInfra",
                profile_revision=(
                    "longmemeval-openrouter-gpt-oss-20b-deepinfra-zdr-shadow-v2"
                ),
                model="openai/gpt-oss-20b",
                # The public starter harness declares 24 agent turns per case.
                # Twelve selected cases therefore require 288 reader requests
                # at the frozen protocol maximum. Scale the original per-turn
                # prompt/completion rails by six; the per-request completion
                # bound remains 2,000 tokens.
                max_requests=288,
                max_prompt_tokens=3_600_000,
                max_completion_tokens=576_000,
                max_total_tokens=4_176_000,
                max_cost_usd_micros=1_500_000,
            ),
            ProviderLanePolicy(
                lane="judge",
                provider="openrouter",
                route_provider="azure",
                receipt_provider="Azure",
                profile_revision="longmemeval-official-gpt4o-azure-zdr-v2",
                model="openai/gpt-4o-2024-08-06",
                max_requests=12,
                max_prompt_tokens=20_000,
                max_completion_tokens=6_000,
                max_total_tokens=26_000,
                max_cost_usd_micros=1_000_000,
            ),
        ),
        embedding_lane=EmbeddingLanePolicy(
            lane="embedding",
            provider="perplexity",
            profile_revision="dittobench-v9-pplx-embed-v1-0.6b-768-v1",
            model="perplexity/pplx-embed-v1-0.6b",
            dimensions=768,
            max_requests=5_000,
            max_input_tokens=5_000_000,
            max_cost_usd_micros=1_000_000,
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
            max_requests=12,
            request_timeout_milliseconds=90_000,
            total_timeout_milliseconds=1_200_000,
        ),
        inference_ablation=AblationVerificationPolicy(
            intervention="inference",
            contract_version=ABLATION_PROFILE_CONTRACT_VERSION,
            threshold_micros=200_000,
            budget=SyntheticBudgetPolicy(
                max_chat_requests=32,
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
                max_embedding_requests=128,
                max_embedding_inputs=512,
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
        "revision": "v9-confirmation-shadow-launch-2026-08-15-zdr-v3",
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
            "daily_cost_microusd": 5_000_000,
            # embedding 5,000 + reader 288 + judge 12.
            "requests_per_bundle": 5_300,
            # embedding 5m + reader 4.176m + judge 26k, rounded up.
            "tokens_per_bundle": 9_300_000,
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
        "launch_manifest_path": (
            "/opt/ditto/confirmation/confirmation_launch_manifest_v9_shadow.json"
        ),
        "longmem_dataset_path": ("/opt/ditto/confirmation/longmemeval_s_cleaned.json"),
        "ablation_dataset_path": (
            "/opt/ditto/confirmation/confirmation_ablation_v9_shadow.json"
        ),
        "sandbox_health_timeout_ms": 180_000,
    }
    installation_raw = _json_bytes(installation)
    return {
        THRESHOLD_MANIFEST: threshold_raw,
        EXECUTION_PROFILE: profile_raw,
        LAUNCH_MANIFEST: launch_raw,
        INSTALLATION: installation_raw,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if checked-in generated assets do not have exact bytes",
    )
    args = parser.parse_args()
    outputs = _outputs()
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
