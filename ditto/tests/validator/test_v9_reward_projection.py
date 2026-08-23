"""Hostile tests for the separate Bench v9 reward receipt."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import bittensor
import httpx
import pytest

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.validator import (
    LedgerEntry,
    LedgerResponse,
    LedgerScoreProof,
    V9BaseEvidence,
    V9ConfirmationEvidenceRoot,
    V9ConfirmationReceipt,
)
from ditto.validator.platform import PlatformClient
from ditto.validator.signing import (
    sign_score,
    sign_v9_confirmation_bundle,
    verify_ledger_entry,
    verify_v9_confirmation_receipt,
)
from ditto.validator.weights import (
    _effective_composite,
    compute_weights,
    filter_weight_confirmed,
)

_VECTOR = (
    Path(__file__).resolve().parents[3]
    / "services/dittobench-api/testdata/v9_base_contract_vectors.json"
)
_AGENT = UUID("10000000-0000-0000-0000-000000000001")
_BUNDLE = UUID("20000000-0000-0000-0000-000000000002")
_TICKET = UUID("30000000-0000-0000-0000-000000000003")
_DEADLINE = datetime(2026, 8, 9, 12, tzinfo=UTC)
_PROFILE = "confirmation-v9-test-1"
_PROFILE_SHA = "b2" * 32
_SETTINGS_SHA = "c3" * 32


def _base(*, quality_micros: int) -> V9BaseEvidence:
    payload = json.loads(_VECTOR.read_text())["vectors"][0]["details"]
    payload["ordinary_composite_micros"] = quality_micros
    payload["ordinary_stderr_micros"] = 12_345
    payload["effective_composite_micros"] = quality_micros
    payload["effective_stderr_micros"] = 12_345
    return V9BaseEvidence.model_validate(payload)


def _policy(
    *,
    base_weight_bps: int = 6_000,
    longmem_weight_bps: int = 4_000,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "revision": "composite-test-1",
        "formula_revision": "weighted-quality-gates-v1",
        "base_weight_bps": base_weight_bps,
        "longmem_weight_bps": longmem_weight_bps,
    }
    payload["checksum"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def _mix_quality(
    ordinary_micros: int,
    longmem_micros: int,
    *,
    base_weight_bps: int,
    longmem_weight_bps: int,
) -> int:
    return (
        base_weight_bps * ordinary_micros + longmem_weight_bps * longmem_micros + 5_000
    ) // 10_000


def _mix_stderr(
    *,
    base_stderr_micros: int = 12_345,
    longmem_stderr_micros: int = 204_124,
    base_weight_bps: int,
    longmem_weight_bps: int,
) -> int:
    with localcontext() as context:
        context.prec = 50
        base_component = (
            Decimal(base_weight_bps) * Decimal(base_stderr_micros) / Decimal(10_000)
        )
        longmem_component = (
            Decimal(longmem_weight_bps)
            * Decimal(longmem_stderr_micros)
            / Decimal(10_000)
        )
        return int(
            (base_component**2 + longmem_component**2)
            .sqrt()
            .quantize(Decimal(1), rounding=ROUND_HALF_UP)
        )


def _longmem(*, artifact_sha256: str, mean_micros: int) -> dict[str, object]:
    return {
        "status": "completed",
        "evidence_sha256": "d4" * 32,
        "latency_ms": 1_234,
        "request_count": 3,
        "input_tokens": 150,
        "output_tokens": 30,
        "provider_cost_microusd": 15_000,
        "synthetic": False,
        "evidence": {
            "schema_version": 2,
            "artifact_sha256": artifact_sha256,
            "bench_version": 9,
            "profile_checksum": "e5" * 32,
            "case_set_digest": "f6" * 32,
            "dataset_revision": "longmem-test-1",
            "dataset_sha256": "a7" * 32,
            "score": {
                "longmem_mean_micros": mean_micros,
                "longmem_stderr_micros": 204_124,
                "case_count": 1,
                "per_capability": [],
            },
            "provider_evidence": [],
        },
    }


def _ablation(
    *,
    artifact_sha256: str,
    intervention: str,
    status: str = "passed",
) -> dict[str, object]:
    inference = intervention == "inference"
    passed = status == "passed"
    return {
        "status": "completed",
        "evidence_sha256": ("b8" if inference else "c9") * 32,
        "latency_ms": 200,
        "request_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "provider_cost_microusd": 0,
        "synthetic": True,
        "evidence": {
            "contract_version": "ablation-test-1",
            "bench_version": 9,
            "artifact_sha256": artifact_sha256,
            "intervention": intervention,
            "mode": "enforce",
            "status": status,
            "reason": "threshold_met" if passed else "observational_drop_not_causal",
            "profile_revision": "ablation-profile-test-1",
            "profile_checksum": "da" * 32,
            "threshold_manifest_sha256": "eb" * 32,
            "coordinator_sha256": "fc" * 32,
            "dataset_sha256": "1d" * 32,
            "case_set_sha256": "2e" * 32,
            "baseline_scores_sha256": "3f" * 32,
            "ablated_scores_sha256": "40" * 32,
            "baseline_mean_micros": 900_000 if not passed else 800_000,
            "ablated_mean_micros": 400_000 if not passed else 500_000,
            "delta_micros": 500_000 if not passed else 300_000,
            "threshold_micros": 200_000,
            "sample_count": 1,
            "affected_call_count": 1,
            "semantic_factor_bps": 10_000 if passed else 0,
            "applied_factor_bps": 10_000 if passed else 0,
            "synthetic_usage": {
                "synthetic": True,
                "intervention": intervention,
                "budget": {
                    "max_chat_requests": 1 if inference else 0,
                    "max_chat_input_bytes": 64 if inference else 0,
                    "max_embedding_requests": 0 if inference else 1,
                    "max_embedding_inputs": 0 if inference else 1,
                    "max_embedding_input_bytes": 0 if inference else 64,
                },
                "chat_attempts": 1 if inference else 0,
                "chat_applied": 1 if inference else 0,
                "chat_input_bytes": 64 if inference else 0,
                "embedding_attempts": 0 if inference else 1,
                "embedding_applied": 0 if inference else 1,
                "embedding_inputs": 0 if inference else 1,
                "embedding_input_bytes": 0 if inference else 64,
                "rejected_requests": 0,
                "budget_exhausted": False,
                "upstream_requests": 0,
                "upstream_input_tokens": 0,
                "upstream_output_tokens": 0,
                "upstream_provider_cost_microusd": 0,
            },
        },
    }


def _entry(
    *,
    agent_id: UUID = _AGENT,
    ordinary_micros: int = 812_345,
    longmem_micros: int = 500_000,
    first_seen: datetime = datetime(2026, 8, 8, tzinfo=UTC),
    efficiency_factor: float | None = None,
    ablation_status: str = "passed",
    base_weight_bps: int = 6_000,
    longmem_weight_bps: int = 4_000,
) -> LedgerEntry:
    base = _base(quality_micros=ordinary_micros)
    validators = [
        bittensor.Keypair.create_from_uri(uri)
        for uri in ("//Alice", "//Bob", "//Charlie")
    ]
    proofs: list[LedgerScoreProof] = []
    for index, validator in enumerate(validators):
        signature = sign_score(
            validator,
            validator_hotkey=validator.ss58_address,
            agent_id=agent_id,
            ticket_deadline=_DEADLINE,
            run_id=base.run_id,
            composite=ordinary_micros / 1_000_000,
            seed=index,
            bench_version=9,
            transcript_sha256=base.transcript_sha256,
            base_evidence_sha256=base.digest_hex(),
        )
        proofs.append(
            LedgerScoreProof(
                validator_hotkey=validator.ss58_address,
                run_id=base.run_id,
                composite=ordinary_micros / 1_000_000,
                seed=index,
                bench_version=9,
                ticket_deadline=_DEADLINE,
                transcript_sha256=base.transcript_sha256,
                base_evidence_sha256=base.digest_hex(),
                base_evidence=base,
                signature=signature,
            )
        )
    proofs.sort(key=lambda proof: (proof.composite, proof.validator_hotkey))
    median = proofs[1]
    root = V9ConfirmationEvidenceRoot.model_validate(
        {
            "schema_version": 1,
            "artifact_sha256": base.artifact_sha256,
            "bench_version": 9,
            "confirmation_profile_revision": _PROFILE,
            "confirmation_profile_checksum": _PROFILE_SHA,
            "settings_revision": 7,
            "settings_checksum": _SETTINGS_SHA,
            "retest_generation": 0,
            "ablation_coordinator_latency_ms": 13,
            "composite_policy": _policy(
                base_weight_bps=base_weight_bps,
                longmem_weight_bps=longmem_weight_bps,
            ),
            "longmemeval": _longmem(
                artifact_sha256=base.artifact_sha256, mean_micros=longmem_micros
            ),
            "inference_ablation": _ablation(
                artifact_sha256=base.artifact_sha256,
                intervention="inference",
                status=ablation_status,
            ),
            "embedding_ablation": _ablation(
                artifact_sha256=base.artifact_sha256,
                intervention="embedding",
                status=ablation_status,
            ),
            "totals": {
                "request_count": 3,
                "input_tokens": 150,
                "output_tokens": 30,
                "provider_cost_microusd": 15_000,
                "latency_ms": 1_247,
            },
        }
    )
    evidence_sha = hashlib.sha256(
        json.dumps(
            root.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    reporter = bittensor.Keypair.create_from_uri("//Dave")
    signature = sign_v9_confirmation_bundle(
        reporter,
        reporter_hotkey=reporter.ss58_address,
        bundle_id=_BUNDLE,
        ticket_id=_TICKET,
        deadline=_DEADLINE,
        artifact_sha256=base.artifact_sha256,
        profile_revision=_PROFILE,
        profile_checksum=_PROFILE_SHA,
        settings_revision=7,
        settings_checksum=_SETTINGS_SHA,
        retest_generation=0,
        evidence_sha256=evidence_sha,
    )
    full_quality = _mix_quality(
        ordinary_micros,
        longmem_micros,
        base_weight_bps=base_weight_bps,
        longmem_weight_bps=longmem_weight_bps,
    )
    full_stderr = _mix_stderr(
        base_weight_bps=base_weight_bps,
        longmem_weight_bps=longmem_weight_bps,
    )
    receipt = V9ConfirmationReceipt(
        mode="enforce",
        result_status="full_confirmed",
        qualification_status="qualified",
        bundle_id=_BUNDLE,
        ticket_id=_TICKET,
        ticket_deadline=_DEADLINE,
        reporter_hotkey=reporter.ss58_address,
        bundle_signature=signature,
        evidence_sha256=evidence_sha,
        evidence_root=root,
        base_evidence_sha256=base.digest_hex(),
        base_quality_micros=ordinary_micros,
        base_stderr_micros=12_345,
        base_model_factor_bps=10_000,
        base_tool_factor_bps=10_000,
        full_quality_micros=full_quality,
        full_stderr_micros=full_stderr,
        semantic_factor_bps=10_000 if ablation_status == "passed" else 0,
        applied_factor_bps=10_000,
        full_effective_micros=full_quality,
        verified_at=datetime(2026, 8, 9, tzinfo=UTC),
    )
    miner = bittensor.Keypair.create_from_uri(
        "//Eve" if agent_id == _AGENT else "//Ferdie"
    )
    return LedgerEntry(
        miner_hotkey=miner.ss58_address,
        agent_id=agent_id,
        composite=median.composite,
        n=114,
        first_seen=first_seen,
        sha256=base.artifact_sha256,
        size_bytes=1_024,
        run_id=median.run_id,
        seed=median.seed,
        validator_hotkey=median.validator_hotkey,
        bench_version=9,
        signature=median.signature,
        score_proofs=proofs,
        composite_stderr=full_stderr / 1_000_000,
        efficiency_factor=efficiency_factor,
        v9_confirmation=receipt,
        status=AgentStatus.SCORED,
    )


def test_v9_receipt_verifies_without_overwriting_ordinary_composite() -> None:
    entry = _entry()
    assert entry.composite == pytest.approx(0.812345)
    assert entry.v9_confirmation is not None
    assert entry.v9_confirmation.full_effective_micros == 687_407
    assert verify_ledger_entry(entry)


def test_failed_enforce_ablation_receipt_keeps_the_longmem_mix() -> None:
    entry = _entry(
        ordinary_micros=882_550, longmem_micros=333_333, ablation_status="failed"
    )
    receipt = entry.v9_confirmation
    assert receipt is not None
    assert receipt.semantic_factor_bps == 0
    assert receipt.applied_factor_bps == 10_000
    assert receipt.full_effective_micros == receipt.full_quality_micros == 662_863
    assert verify_v9_confirmation_receipt(entry)
    assert verify_ledger_entry(entry)


def test_failed_enforce_ablation_receipt_keeps_the_live_70_30_mix() -> None:
    # harry.xiv 27e7fa08: base 0.882550, LongMem 4/12 = 0.333333.
    entry = _entry(
        ordinary_micros=882_550,
        longmem_micros=333_333,
        ablation_status="failed",
        base_weight_bps=7_000,
        longmem_weight_bps=3_000,
    )
    receipt = entry.v9_confirmation
    assert receipt is not None
    assert receipt.semantic_factor_bps == 0
    assert receipt.applied_factor_bps == 10_000
    assert receipt.full_effective_micros == receipt.full_quality_micros == 717_785
    assert verify_v9_confirmation_receipt(entry)
    assert verify_ledger_entry(entry)


def test_base_only_v9_enforce_gate_is_not_confirmation_authority() -> None:
    entry = _entry().model_copy(update={"v9_confirmation": None})
    assert entry.score_proofs[1].base_evidence is not None
    assert entry.score_proofs[1].base_evidence.score_gates.rollout_mode == "enforce"
    # Transport accepts the cryptographically valid row so one unconfirmed v9
    # result cannot abort the whole mixed-version ledger fetch. Reward authority
    # is applied per entry at the weight fold instead.
    assert verify_ledger_entry(entry)
    assert filter_weight_confirmed([entry]) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("confirmation_history", [{"seed": 1, "composite": 1.0}]),
        ("confirmation_seeds", [1, 2]),
        ("confirmation_composites", [1.0, 1.0]),
    ],
)
def test_v9_receipt_rejects_legacy_confirmation_projection(
    field: str, value: object
) -> None:
    entry = _entry().model_copy(update={field: value})
    assert not verify_ledger_entry(entry)


def test_v9_receipt_disables_legacy_paired_seed_path() -> None:
    entry = _entry().model_copy(
        update={
            "confirmation_seeds": [1, 2],
            "confirmation_composites": [0.99, 1.0],
        }
    )
    from ditto.validator.weights import _entry_seed_composites

    assert _entry_seed_composites(entry) is None


async def test_v9_receipt_survives_serialized_http_ledger_round_trip() -> None:
    entry = _entry()
    payload = LedgerResponse(
        entries=[entry],
        v9_confirmation_mode="enforce",
        count=1,
        generated_at=datetime(2026, 8, 9, tzinfo=UTC),
    ).model_dump(mode="json")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    caller = bittensor.Keypair.create_from_uri("//Alice")
    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=caller.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        received = await PlatformClient(config, http, caller).get_ledger()  # type: ignore[arg-type]

    assert received.entries[0].v9_confirmation is not None
    assert received.entries[0].v9_confirmation.bundle_id == _BUNDLE
    assert received.entries[0].v9_confirmation.ticket_deadline == _DEADLINE


@pytest.mark.parametrize("tamper", ["derived", "root", "policy"])
def test_v9_receipt_tampering_fails_closed(tamper: str) -> None:
    entry = _entry()
    assert entry.v9_confirmation is not None
    receipt = entry.v9_confirmation
    if tamper == "derived":
        changed = receipt.model_copy(
            update={"full_effective_micros": receipt.full_effective_micros + 1}
        )
    else:
        root_payload = receipt.evidence_root.model_dump(mode="json")
        if tamper == "root":
            root_payload["longmemeval"]["evidence"]["score"]["longmem_mean_micros"] += 1
        else:
            root_payload["composite_policy"]["base_weight_bps"] = 7_000
            root_payload["composite_policy"]["longmem_weight_bps"] = 3_000
        changed = receipt.model_copy(
            update={
                "evidence_root": V9ConfirmationEvidenceRoot.model_validate(root_payload)
            }
        )
    tampered = entry.model_copy(update={"v9_confirmation": changed})
    assert not verify_ledger_entry(tampered)


def test_weights_use_verified_full_composite_for_v9_enforce() -> None:
    ordinary_leader = _entry()
    full_leader = _entry(
        agent_id=UUID("10000000-0000-0000-0000-000000000099"),
        ordinary_micros=750_000,
        longmem_micros=1_000_000,
        first_seen=ordinary_leader.first_seen + timedelta(minutes=1),
    )
    assert full_leader.composite < ordinary_leader.composite
    assert full_leader.v9_confirmation is not None
    assert full_leader.v9_confirmation.full_effective_micros == 850_000
    weights = compute_weights(
        [ordinary_leader, full_leader],
        margin=0.005,
        tail_size=1,
        rank_shares=(0.8, 0.2),
    )
    assert weights[full_leader.miner_hotkey] == pytest.approx(0.8)


def test_weights_rank_reader_used_mix_above_unused_reader_zero() -> None:
    unused_reader = _entry(
        ordinary_micros=893_494,
        longmem_micros=0,
        ablation_status="failed",
        base_weight_bps=7_000,
        longmem_weight_bps=3_000,
    )
    harry = _entry(
        agent_id=UUID("10000000-0000-0000-0000-000000000099"),
        ordinary_micros=882_550,
        longmem_micros=333_333,
        ablation_status="failed",
        first_seen=unused_reader.first_seen + timedelta(minutes=1),
        base_weight_bps=7_000,
        longmem_weight_bps=3_000,
    )
    assert unused_reader.v9_confirmation is not None
    assert harry.v9_confirmation is not None
    assert unused_reader.v9_confirmation.full_effective_micros == 625_446
    assert harry.v9_confirmation.full_effective_micros == 717_785
    assert harry.composite < unused_reader.composite
    weights = compute_weights(
        [unused_reader, harry],
        margin=0.005,
        tail_size=1,
        rank_shares=(0.8, 0.2),
    )
    assert weights[harry.miner_hotkey] == pytest.approx(0.8)


@pytest.mark.parametrize("factor", [0.85, 1.0, 1.1])
def test_v9_efficiency_factor_applies_after_verified_full_quality(
    factor: float,
) -> None:
    entry = _entry(efficiency_factor=factor)
    assert entry.v9_confirmation is not None

    full_quality = entry.v9_confirmation.full_effective_micros / 1_000_000
    expected = (
        full_quality * factor
        if factor <= 1.0
        else full_quality + (factor - 1.0) * (1.0 - full_quality)
    )
    assert _effective_composite(entry) == pytest.approx(expected)


def test_v9_efficiency_factor_preserves_perfect_quality() -> None:
    entry = _entry(
        ordinary_micros=1_000_000,
        longmem_micros=1_000_000,
        efficiency_factor=1.1,
    )

    assert _effective_composite(entry) == 1.0


def test_v9_full_quality_replays_a_frozen_legacy_curve_bonus() -> None:
    entry = _entry().model_copy(update={"efficiency_bonus": 0.1})
    assert entry.v9_confirmation is not None

    full_quality = entry.v9_confirmation.full_effective_micros / 1_000_000
    assert _effective_composite(entry) == pytest.approx(full_quality * 1.1)


async def test_v9_efficiency_factor_survives_serialized_http_ledger_round_trip() -> (
    None
):
    entry = _entry(efficiency_factor=0.85)
    payload = LedgerResponse(
        entries=[entry],
        v9_confirmation_mode="enforce",
        count=1,
        generated_at=datetime(2026, 8, 9, tzinfo=UTC),
    ).model_dump(mode="json")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    caller = bittensor.Keypair.create_from_uri("//Alice")
    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=caller.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        received = await PlatformClient(config, http, caller).get_ledger()  # type: ignore[arg-type]

    assert received.entries[0].efficiency_factor == pytest.approx(0.85)
