"""Hostile contract tests for the private Bench v9 confirmation transport.

This lane is intentionally disjoint from canonical scoring and the legacy
top-five shared-seed lane.  These tests lock that separation at the validator
HTTP boundary and lock every field covered by the three signing domains.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import bittensor
import httpx
import pytest
from pydantic import ValidationError

from ditto.api_models.validator import (
    ArtifactResponse,
    V9ConfirmationCompositePolicy,
    V9ConfirmationEvidenceRoot,
)
from ditto.api_models.validator_confirmation import (
    AblationDimensionEnvelope,
    ConfirmationUsageTotals,
    LongMemDimensionEnvelope,
    V9ConfirmationClaimRequest,
    V9ConfirmationCompletionReport,
    V9ConfirmationJobResponse,
    V9ConfirmationPreparedReport,
    V9ConfirmationPrepareRequest,
    V9ConfirmationRawDimension,
    V9ConfirmationScorerReadiness,
    V9ConfirmationScorerResult,
    V9ConfirmationSubmitRequest,
    V9ConfirmationSubmitResponse,
)
from ditto.validator import worker as worker_module
from ditto.validator.errors import DittobenchError, PlatformError
from ditto.validator.platform import PlatformClient
from ditto.validator.signing import (
    rebuild_v9_confirmation_evidence_root,
    sign_v9_confirmation_artifact_request,
    sign_v9_confirmation_bundle,
    sign_v9_confirmation_claim,
    sign_v9_confirmation_prepare,
    v9_confirmation_artifact_signing_message,
    v9_confirmation_bundle_signing_message,
    v9_confirmation_claim_signing_message,
    v9_confirmation_fail_signing_message,
    v9_confirmation_prepare_signing_message,
    v9_confirmation_prepare_wire_sha256,
)
from ditto.validator.worker import ValidatorWorker
from ditto_screening_protocol.confirmation import canonical_json, evidence_digest
from ditto_screening_protocol.confirmation_wire import (
    completion_report_from_go_dimensions,
)

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_BUNDLE_ID = UUID("10000000-0000-0000-0000-000000000001")
_TICKET_ID = UUID("20000000-0000-0000-0000-000000000002")
_RESERVATION_ID = UUID("30000000-0000-0000-0000-000000000003")
_AGENT_ID = UUID("40000000-0000-0000-0000-000000000004")
_NONCE = UUID("50000000-0000-0000-0000-000000000005")
_REQUESTED_AT = datetime(
    2026,
    8,
    8,
    13,
    14,
    15,
    123456,
    tzinfo=timezone(timedelta(hours=-4)),
)
_DEADLINE = datetime(2026, 8, 9, 1, 2, 3, 456789, tzinfo=timezone(timedelta(hours=2)))
_ARTIFACT_SHA = "a" * 64
_PROFILE_REVISION = "confirmation-v9-2026-08-08"
_PROFILE_CHECKSUM = "b2" * 32
_SETTINGS_CHECKSUM = "c3" * 32
_EVIDENCE_SHA = "d4" * 32


def _config(hotkey: str = _HOTKEY) -> Any:
    return cast(
        Any,
        SimpleNamespace(
            platform_api_url="https://platform.test/",
            validator_hotkey=hotkey,
        ),
    )


def _execution_profile() -> dict[str, Any]:
    budget = {
        "max_chat_requests": 17,
        "max_chat_input_bytes": 18_000,
        "max_embedding_requests": 19,
        "max_embedding_inputs": 20,
        "max_embedding_input_bytes": 21_000,
    }
    return {
        "schema_version": 1,
        "revision": _PROFILE_REVISION,
        "checksum": _PROFILE_CHECKSUM,
        "longmem_profile_revision": "longmem-profile-v1",
        "longmem_profile_checksum": "e5" * 32,
        "longmem_dataset_revision": "longmem-v1-pinned",
        "longmem_dataset_sha256": "f6" * 32,
        "longmem_selector_revision": "longmemeval-s-stratified-sha256-v1",
        "longmem_selection_seed": 8675309,
        "longmem_cases_per_capability": 2,
        "longmem_seed_batch_pairs": 3,
        "longmem_projection_key_sha256": "e" * 64,
        "provider_lanes": [
            {
                "lane": "longmem-primary",
                "provider": "trusted-provider",
                "profile_revision": "provider-profile-v1",
                "model": "openai/gpt-oss-20b",
                "max_requests": 9,
                "max_prompt_tokens": 10_000,
                "max_completion_tokens": 2_000,
                "max_total_tokens": 12_000,
                "max_cost_usd_micros": 42_000,
            }
        ],
        "ablation_profile_revision": "ablation-profile-v1",
        "ablation_profile_checksum": "01" * 32,
        "ablation_dataset_sha256": "02" * 32,
        "ablation_threshold_manifest_sha256": "03" * 32,
        "ablation_selection_key_sha256": "04" * 32,
        "ablation_projection_key_sha256": "05" * 32,
        "ablation_coordinator_policy": {
            "sample_size": 4,
            "max_attempts": 2,
            "max_requests": 24,
            "request_timeout_milliseconds": 1_000,
            "total_timeout_milliseconds": 5_000,
        },
        "inference_ablation": {
            "intervention": "inference",
            "contract_version": "ablation-v1",
            "threshold_micros": 200_000,
            "budget": budget,
        },
        "embedding_ablation": {
            "intervention": "embedding",
            "contract_version": "ablation-v1",
            "threshold_micros": 100_000,
            "budget": budget,
        },
        "composite": {
            "schema_version": 1,
            "revision": "confirmation-composite-v1",
            "formula_revision": "weighted-quality-gates-v1",
            "base_weight_bps": 7_500,
            "longmem_weight_bps": 2_500,
            "checksum": "09" * 32,
        },
    }


def _job_payload() -> dict[str, Any]:
    return {
        "purpose": "v9_confirmation_bundle",
        "bundle_id": str(_BUNDLE_ID),
        "ticket_id": str(_TICKET_ID),
        "reservation_id": str(_RESERVATION_ID),
        "agent_id": str(_AGENT_ID),
        "slot_id": "slot-3",
        "deadline": _DEADLINE.isoformat(),
        "artifact_sha256": _ARTIFACT_SHA,
        "bench_version": 9,
        "settings_revision": 7,
        "settings_checksum": _SETTINGS_CHECKSUM,
        "retest_generation": 2,
        "mode": "shadow",
        "per_bundle_request_cap": 100,
        "per_bundle_token_cap": 250_000,
        "execution_profile": _execution_profile(),
    }


def _job() -> V9ConfirmationJobResponse:
    return V9ConfirmationJobResponse.model_validate(_job_payload())


def _report() -> V9ConfirmationCompletionReport:
    prepared, _, _ = _worker_prepared_report(_job())
    return V9ConfirmationCompletionReport(
        longmemeval=prepared.longmemeval,
        inference_ablation=prepared.inference_ablation,
        embedding_ablation=prepared.embedding_ablation,
        ablation_coordinator_latency_ms=37,
        bundle_signature="ab" * 64,
    )


def _scorer_result() -> V9ConfirmationScorerResult:
    fixture_path = (
        Path(__file__).resolve().parents[3]
        / "services"
        / "dittobench-api"
        / "internal"
        / "confirmationwire"
        / "testdata"
        / "go_confirmation_evidence_v9.json"
    )
    fixture = json.loads(fixture_path.read_text())
    dimensions = {
        name: V9ConfirmationRawDimension.model_validate(fixture[name])
        for name in (
            "longmemeval",
            "inference_ablation",
            "embedding_ablation",
        )
    }
    latency = 37
    wire_sha256 = v9_confirmation_prepare_wire_sha256(
        ablation_coordinator_latency_ms=latency,
        longmemeval=dimensions["longmemeval"].model_dump(mode="json"),
        inference_ablation=dimensions["inference_ablation"].model_dump(mode="json"),
        embedding_ablation=dimensions["embedding_ablation"].model_dump(mode="json"),
    )
    return V9ConfirmationScorerResult(
        longmemeval=dimensions["longmemeval"],
        inference_ablation=dimensions["inference_ablation"],
        embedding_ablation=dimensions["embedding_ablation"],
        ablation_coordinator_latency_ms=latency,
        evidence_sha256=wire_sha256,
    )


def _prepared_payload() -> dict[str, Any]:
    prepared, _, _ = _worker_prepared_report(_job())
    payload = prepared.model_dump(mode="json")
    # Transport-only tests exercise identity binding independently from the
    # worker's signed-root replay, so keep their recognizable server digest.
    payload["evidence_sha256"] = _EVIDENCE_SHA
    return payload


def _prepared() -> V9ConfirmationPreparedReport:
    return V9ConfirmationPreparedReport.model_validate(_prepared_payload())


def _worker_prepared_report(
    job: V9ConfirmationJobResponse,
) -> tuple[
    V9ConfirmationPreparedReport,
    V9ConfirmationEvidenceRoot,
    str,
]:
    scorer_result = _scorer_result()
    normalized = completion_report_from_go_dimensions(
        ablation_coordinator_latency_ms=(scorer_result.ablation_coordinator_latency_ms),
        longmemeval=scorer_result.longmemeval.model_dump(mode="json"),
        inference_ablation=scorer_result.inference_ablation.model_dump(mode="json"),
        embedding_ablation=scorer_result.embedding_ablation.model_dump(mode="json"),
    )
    longmemeval = normalized.longmemeval
    latency = scorer_result.ablation_coordinator_latency_ms
    expected_root = V9ConfirmationEvidenceRoot(
        schema_version=1,
        artifact_sha256=job.artifact_sha256,
        bench_version=9,
        confirmation_profile_revision=job.execution_profile.revision,
        confirmation_profile_checksum=job.execution_profile.checksum,
        settings_revision=job.settings_revision,
        settings_checksum=job.settings_checksum,
        retest_generation=job.retest_generation,
        ablation_coordinator_latency_ms=latency,
        composite_policy=V9ConfirmationCompositePolicy.model_validate(
            job.execution_profile.composite.model_dump(mode="json")
        ),
        longmemeval=longmemeval,
        inference_ablation=normalized.inference_ablation,
        embedding_ablation=normalized.embedding_ablation,
        totals=ConfirmationUsageTotals(
            request_count=longmemeval.request_count,
            input_tokens=longmemeval.input_tokens,
            output_tokens=longmemeval.output_tokens,
            provider_cost_microusd=longmemeval.provider_cost_microusd,
            latency_ms=longmemeval.latency_ms + latency,
        ),
    )
    expected_digest = hashlib.sha256(canonical_json(expected_root)).hexdigest()
    prepared = V9ConfirmationPreparedReport(
        bundle_id=job.bundle_id,
        ticket_id=job.ticket_id,
        ablation_coordinator_latency_ms=latency,
        longmemeval=longmemeval,
        inference_ablation=normalized.inference_ablation,
        embedding_ablation=normalized.embedding_ablation,
        evidence_sha256=expected_digest,
    )
    return prepared, expected_root, expected_digest


def _artifact_payload() -> dict[str, Any]:
    image_sha = "ef" * 32
    return {
        "agent_id": str(_AGENT_ID),
        "sha256": _ARTIFACT_SHA,
        "download_url": "https://storage.test/v9-artifact",
        "expires_at": datetime(2026, 8, 9, tzinfo=UTC).isoformat(),
        "bench_version": 9,
        "screening_policy_version": 9,
        "screened_image_url": "https://storage.test/v9-screened-image",
        "screened_image_sha256": image_sha,
        "screened_image_size_bytes": 42_000,
        "screened_image_id": f"sha256:{image_sha}",
        "screened_image_ref": "ditto-screen/agent:v9",
    }


def _submit_response(*, replayed: bool = False) -> dict[str, Any]:
    return {
        "bundle_id": str(_BUNDLE_ID),
        "ticket_id": str(_TICKET_ID),
        "accepted": True,
        "state": "completed",
        "qualification_status": "qualified",
        "evidence_sha256": _EVIDENCE_SHA,
        "replayed": replayed,
    }


def test_claim_signing_domain_is_byte_exact_and_normalizes_to_utc() -> None:
    message = v9_confirmation_claim_signing_message(
        validator_hotkey=_HOTKEY,
        slot_id="slot-3",
        profile_revision=_PROFILE_REVISION,
        profile_checksum=_PROFILE_CHECKSUM,
        nonce=_NONCE,
        requested_at=_REQUESTED_AT,
    )

    assert (
        message
        == (
            "validator-v9-confirmation-claim:v1:"
            f"{_HOTKEY}:slot-3:{_PROFILE_REVISION}:{_PROFILE_CHECKSUM}:"
            f"{_NONCE}:2026-08-08T17:14:15.123456Z"
        ).encode()
    )


def test_artifact_signing_domain_is_byte_exact_and_normalizes_to_utc() -> None:
    message = v9_confirmation_artifact_signing_message(
        validator_hotkey=_HOTKEY,
        bundle_id=_BUNDLE_ID,
        ticket_id=_TICKET_ID,
        nonce=_NONCE,
        requested_at=_REQUESTED_AT,
    )

    assert (
        message
        == (
            "validator-v9-confirmation-artifact:v1:"
            f"{_HOTKEY}:{_BUNDLE_ID}:{_TICKET_ID}:{_NONCE}:"
            "2026-08-08T17:14:15.123456Z"
        ).encode()
    )


def test_failure_signing_domain_is_byte_exact_and_binds_reason() -> None:
    message = v9_confirmation_fail_signing_message(
        validator_hotkey=_HOTKEY,
        bundle_id=_BUNDLE_ID,
        ticket_id=_TICKET_ID,
        reason="execution_failed",
        nonce=_NONCE,
        requested_at=_REQUESTED_AT,
    )

    assert (
        message
        == (
            "validator-v9-confirmation-fail:v1:"
            f"{_HOTKEY}:{_BUNDLE_ID}:{_TICKET_ID}:execution_failed:{_NONCE}:"
            "2026-08-08T17:14:15.123456Z"
        ).encode()
    )


def test_bundle_signing_domain_is_byte_exact_and_binds_bench_nine() -> None:
    message = v9_confirmation_bundle_signing_message(
        reporter_hotkey=_HOTKEY,
        bundle_id=_BUNDLE_ID,
        ticket_id=_TICKET_ID,
        deadline=_DEADLINE,
        artifact_sha256=_ARTIFACT_SHA,
        profile_revision=_PROFILE_REVISION,
        profile_checksum=_PROFILE_CHECKSUM,
        settings_revision=7,
        settings_checksum=_SETTINGS_CHECKSUM,
        retest_generation=2,
        evidence_sha256=_EVIDENCE_SHA,
    )

    assert (
        message
        == (
            "validator-v9-confirmation:v1:"
            f"{_HOTKEY}:{_BUNDLE_ID}:{_TICKET_ID}:"
            "2026-08-08T23:02:03.456789Z:"
            f"{_ARTIFACT_SHA}:9:{_PROFILE_REVISION}:{_PROFILE_CHECKSUM}:"
            f"7:{_SETTINGS_CHECKSUM}:2:{_EVIDENCE_SHA}"
        ).encode()
    )


def test_prepare_wire_digest_is_canonical_and_byte_exact() -> None:
    dimensions = {
        "longmemeval": {
            "go_evidence_sha256": "91" * 32,
            "latency_ms": 101,
            "evidence": {"score": 0.75, "completed": True},
        },
        "inference_ablation": {
            "go_evidence_sha256": "92" * 32,
            "latency_ms": 102,
            "evidence": {"values": [3, 1], "kind": "inference"},
        },
        "embedding_ablation": {
            "go_evidence_sha256": "93" * 32,
            "latency_ms": 103,
            "evidence": {"z": 2, "a": 1},
        },
    }
    digest = v9_confirmation_prepare_wire_sha256(
        ablation_coordinator_latency_ms=37,
        **dimensions,
    )

    # Locks the language-independent ASCII identity, including dimension order.
    assert digest == "8273b64aa8408f9e98b68c18b0e8f70b87f3d864f45e1485cae7990cce107df6"

    reordered = v9_confirmation_prepare_wire_sha256(
        ablation_coordinator_latency_ms=37,
        longmemeval={
            "evidence": {"completed": True, "score": 0.75},
            "latency_ms": 101,
            "go_evidence_sha256": "91" * 32,
        },
        inference_ablation={
            "evidence": {"kind": "inference", "values": [3, 1]},
            "latency_ms": 102,
            "go_evidence_sha256": "92" * 32,
        },
        embedding_ablation={
            "evidence": {"a": 1, "z": 2},
            "latency_ms": 103,
            "go_evidence_sha256": "93" * 32,
        },
    )
    assert reordered == digest


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("ablation_coordinator_latency_ms", 38),
        (
            "longmemeval",
            {"go_evidence_sha256": "94" * 32, "latency_ms": 101},
        ),
        (
            "inference_ablation",
            {"go_evidence_sha256": "92" * 32, "latency_ms": 104},
        ),
        (
            "embedding_ablation",
            {"go_evidence_sha256": "95" * 32, "latency_ms": 105},
        ),
    ],
)
def test_prepare_wire_digest_binds_every_top_level_input(
    field: str, replacement: Any
) -> None:
    values: dict[str, object] = {
        "ablation_coordinator_latency_ms": 37,
        "longmemeval": {"go_evidence_sha256": "91" * 32, "latency_ms": 101},
        "inference_ablation": {
            "go_evidence_sha256": "92" * 32,
            "latency_ms": 102,
        },
        "embedding_ablation": {
            "go_evidence_sha256": "93" * 32,
            "latency_ms": 103,
        },
    }

    def wire_digest() -> str:
        return v9_confirmation_prepare_wire_sha256(
            ablation_coordinator_latency_ms=cast(
                int, values["ablation_coordinator_latency_ms"]
            ),
            longmemeval=cast(dict[str, object], values["longmemeval"]),
            inference_ablation=cast(dict[str, object], values["inference_ablation"]),
            embedding_ablation=cast(dict[str, object], values["embedding_ablation"]),
        )

    original = wire_digest()
    values[field] = replacement

    assert wire_digest() != original


def test_prepare_wire_digest_ignores_language_specific_evidence_json() -> None:
    original = _scorer_result().model_dump()
    changed = copy.deepcopy(original)
    changed["longmemeval"]["evidence"] = {"float": 1e-7}

    assert v9_confirmation_prepare_wire_sha256(
        ablation_coordinator_latency_ms=original["ablation_coordinator_latency_ms"],
        longmemeval=original["longmemeval"],
        inference_ablation=original["inference_ablation"],
        embedding_ablation=original["embedding_ablation"],
    ) == v9_confirmation_prepare_wire_sha256(
        ablation_coordinator_latency_ms=changed["ablation_coordinator_latency_ms"],
        longmemeval=changed["longmemeval"],
        inference_ablation=changed["inference_ablation"],
        embedding_ablation=changed["embedding_ablation"],
    )


@pytest.mark.parametrize(
    "dimension",
    [
        {"go_evidence_sha256": "AA" * 32, "latency_ms": 1},
        {"go_evidence_sha256": "zz" * 32, "latency_ms": 1},
        {"go_evidence_sha256": "91" * 32, "latency_ms": True},
        {"go_evidence_sha256": "91" * 32, "latency_ms": -1},
    ],
)
def test_prepare_wire_digest_rejects_noncanonical_native_identity(
    dimension: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        v9_confirmation_prepare_wire_sha256(
            ablation_coordinator_latency_ms=37,
            longmemeval=dimension,
            inference_ablation={"go_evidence_sha256": "92" * 32, "latency_ms": 2},
            embedding_ablation={"go_evidence_sha256": "93" * 32, "latency_ms": 3},
        )


def test_prepare_signing_domain_is_byte_exact_and_normalizes_to_utc() -> None:
    message = v9_confirmation_prepare_signing_message(
        validator_hotkey=_HOTKEY,
        bundle_id=_BUNDLE_ID,
        ticket_id=_TICKET_ID,
        wire_sha256=_EVIDENCE_SHA,
        nonce=_NONCE,
        requested_at=_REQUESTED_AT,
    )

    assert (
        message
        == (
            "validator-v9-confirmation-prepare:v1:"
            f"{_HOTKEY}:{_BUNDLE_ID}:{_TICKET_ID}:{_EVIDENCE_SHA}:"
            f"{_NONCE}:2026-08-08T17:14:15.123456Z"
        ).encode()
    )


_PREPARE_FIELDS = [
    ("validator_hotkey", "5FHneW46xGXgs5mUiveU4sbTyGBzmstWkBrhZQvQmCgmH6kT"),
    ("bundle_id", UUID("10000000-0000-0000-0000-000000000009")),
    ("ticket_id", UUID("20000000-0000-0000-0000-000000000009")),
    ("wire_sha256", "14" * 32),
    ("nonce", UUID("50000000-0000-0000-0000-000000000009")),
    ("requested_at", _REQUESTED_AT + timedelta(microseconds=1)),
]


@pytest.mark.parametrize(("field", "tampered"), _PREPARE_FIELDS)
def test_prepare_signature_rejects_each_field_tamper(field: str, tampered: Any) -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    values: dict[str, object] = {
        "validator_hotkey": keypair.ss58_address,
        "bundle_id": _BUNDLE_ID,
        "ticket_id": _TICKET_ID,
        "wire_sha256": _EVIDENCE_SHA,
        "nonce": _NONCE,
        "requested_at": _REQUESTED_AT,
    }
    signature = sign_v9_confirmation_prepare(
        keypair,
        validator_hotkey=keypair.ss58_address,
        bundle_id=_BUNDLE_ID,
        ticket_id=_TICKET_ID,
        wire_sha256=_EVIDENCE_SHA,
        nonce=_NONCE,
        requested_at=_REQUESTED_AT,
    )
    values[field] = tampered

    assert not keypair.verify(
        v9_confirmation_prepare_signing_message(
            validator_hotkey=cast(str, values["validator_hotkey"]),
            bundle_id=cast(UUID, values["bundle_id"]),
            ticket_id=cast(UUID, values["ticket_id"]),
            wire_sha256=cast(str, values["wire_sha256"]),
            nonce=cast(UUID, values["nonce"]),
            requested_at=cast(datetime, values["requested_at"]),
        ),
        bytes.fromhex(signature),
    )


_CLAIM_FIELDS = [
    ("validator_hotkey", "5FHneW46xGXgs5mUiveU4sbTyGBzmstWkBrhZQvQmCgmH6kT"),
    ("slot_id", "slot-4"),
    ("profile_revision", "other-profile"),
    ("profile_checksum", "ff" * 32),
    ("nonce", UUID("50000000-0000-0000-0000-000000000006")),
    ("requested_at", _REQUESTED_AT + timedelta(microseconds=1)),
]


@pytest.mark.parametrize(("field", "tampered"), _CLAIM_FIELDS)
def test_claim_signature_rejects_each_field_tamper(field: str, tampered: Any) -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    values: dict[str, object] = {
        "validator_hotkey": keypair.ss58_address,
        "slot_id": "slot-3",
        "profile_revision": _PROFILE_REVISION,
        "profile_checksum": _PROFILE_CHECKSUM,
        "nonce": _NONCE,
        "requested_at": _REQUESTED_AT,
    }
    signature = sign_v9_confirmation_claim(
        keypair,
        validator_hotkey=keypair.ss58_address,
        slot_id="slot-3",
        profile_revision=_PROFILE_REVISION,
        profile_checksum=_PROFILE_CHECKSUM,
        nonce=_NONCE,
        requested_at=_REQUESTED_AT,
    )
    values[field] = tampered

    assert not keypair.verify(
        v9_confirmation_claim_signing_message(
            validator_hotkey=cast(str, values["validator_hotkey"]),
            slot_id=cast(str, values["slot_id"]),
            profile_revision=cast(str, values["profile_revision"]),
            profile_checksum=cast(str, values["profile_checksum"]),
            nonce=cast(UUID, values["nonce"]),
            requested_at=cast(datetime, values["requested_at"]),
        ),
        bytes.fromhex(signature),
    )


_ARTIFACT_FIELDS = [
    ("validator_hotkey", "5FHneW46xGXgs5mUiveU4sbTyGBzmstWkBrhZQvQmCgmH6kT"),
    ("bundle_id", UUID("10000000-0000-0000-0000-000000000009")),
    ("ticket_id", UUID("20000000-0000-0000-0000-000000000009")),
    ("nonce", UUID("50000000-0000-0000-0000-000000000009")),
    ("requested_at", _REQUESTED_AT + timedelta(seconds=1)),
]


@pytest.mark.parametrize(("field", "tampered"), _ARTIFACT_FIELDS)
def test_artifact_signature_rejects_each_field_tamper(
    field: str, tampered: Any
) -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    values: dict[str, object] = {
        "validator_hotkey": keypair.ss58_address,
        "bundle_id": _BUNDLE_ID,
        "ticket_id": _TICKET_ID,
        "nonce": _NONCE,
        "requested_at": _REQUESTED_AT,
    }
    signature = sign_v9_confirmation_artifact_request(
        keypair,
        validator_hotkey=keypair.ss58_address,
        bundle_id=_BUNDLE_ID,
        ticket_id=_TICKET_ID,
        nonce=_NONCE,
        requested_at=_REQUESTED_AT,
    )
    values[field] = tampered

    assert not keypair.verify(
        v9_confirmation_artifact_signing_message(
            validator_hotkey=cast(str, values["validator_hotkey"]),
            bundle_id=cast(UUID, values["bundle_id"]),
            ticket_id=cast(UUID, values["ticket_id"]),
            nonce=cast(UUID, values["nonce"]),
            requested_at=cast(datetime, values["requested_at"]),
        ),
        bytes.fromhex(signature),
    )


_BUNDLE_FIELDS = [
    ("reporter_hotkey", "5FHneW46xGXgs5mUiveU4sbTyGBzmstWkBrhZQvQmCgmH6kT"),
    ("bundle_id", UUID("10000000-0000-0000-0000-000000000009")),
    ("ticket_id", UUID("20000000-0000-0000-0000-000000000009")),
    ("deadline", _DEADLINE + timedelta(microseconds=1)),
    ("artifact_sha256", "10" * 32),
    ("profile_revision", "other-profile"),
    ("profile_checksum", "11" * 32),
    ("settings_revision", 8),
    ("settings_checksum", "12" * 32),
    ("retest_generation", 3),
    ("evidence_sha256", "13" * 32),
]


@pytest.mark.parametrize(("field", "tampered"), _BUNDLE_FIELDS)
def test_bundle_signature_rejects_each_field_tamper(field: str, tampered: Any) -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    values: dict[str, object] = {
        "reporter_hotkey": keypair.ss58_address,
        "bundle_id": _BUNDLE_ID,
        "ticket_id": _TICKET_ID,
        "deadline": _DEADLINE,
        "artifact_sha256": _ARTIFACT_SHA,
        "profile_revision": _PROFILE_REVISION,
        "profile_checksum": _PROFILE_CHECKSUM,
        "settings_revision": 7,
        "settings_checksum": _SETTINGS_CHECKSUM,
        "retest_generation": 2,
        "evidence_sha256": _EVIDENCE_SHA,
    }
    signature = sign_v9_confirmation_bundle(
        keypair,
        reporter_hotkey=keypair.ss58_address,
        bundle_id=_BUNDLE_ID,
        ticket_id=_TICKET_ID,
        deadline=_DEADLINE,
        artifact_sha256=_ARTIFACT_SHA,
        profile_revision=_PROFILE_REVISION,
        profile_checksum=_PROFILE_CHECKSUM,
        settings_revision=7,
        settings_checksum=_SETTINGS_CHECKSUM,
        retest_generation=2,
        evidence_sha256=_EVIDENCE_SHA,
    )
    values[field] = tampered

    assert not keypair.verify(
        v9_confirmation_bundle_signing_message(
            reporter_hotkey=cast(str, values["reporter_hotkey"]),
            bundle_id=cast(UUID, values["bundle_id"]),
            ticket_id=cast(UUID, values["ticket_id"]),
            deadline=cast(datetime, values["deadline"]),
            artifact_sha256=cast(str, values["artifact_sha256"]),
            profile_revision=cast(str, values["profile_revision"]),
            profile_checksum=cast(str, values["profile_checksum"]),
            settings_revision=cast(int, values["settings_revision"]),
            settings_checksum=cast(str, values["settings_checksum"]),
            retest_generation=cast(int, values["retest_generation"]),
            evidence_sha256=cast(str, values["evidence_sha256"]),
        ),
        bytes.fromhex(signature),
    )


@pytest.mark.parametrize(
    "builder,kwargs",
    [
        (
            v9_confirmation_claim_signing_message,
            {
                "validator_hotkey": _HOTKEY,
                "slot_id": "slot-3",
                "profile_revision": _PROFILE_REVISION,
                "profile_checksum": _PROFILE_CHECKSUM,
                "nonce": _NONCE,
                "requested_at": _REQUESTED_AT.replace(tzinfo=None),
            },
        ),
        (
            v9_confirmation_artifact_signing_message,
            {
                "validator_hotkey": _HOTKEY,
                "bundle_id": _BUNDLE_ID,
                "ticket_id": _TICKET_ID,
                "nonce": _NONCE,
                "requested_at": _REQUESTED_AT.replace(tzinfo=None),
            },
        ),
        (
            v9_confirmation_bundle_signing_message,
            {
                "reporter_hotkey": _HOTKEY,
                "bundle_id": _BUNDLE_ID,
                "ticket_id": _TICKET_ID,
                "deadline": _DEADLINE.replace(tzinfo=None),
                "artifact_sha256": _ARTIFACT_SHA,
                "profile_revision": _PROFILE_REVISION,
                "profile_checksum": _PROFILE_CHECKSUM,
                "settings_revision": 7,
                "settings_checksum": _SETTINGS_CHECKSUM,
                "retest_generation": 2,
                "evidence_sha256": _EVIDENCE_SHA,
            },
        ),
    ],
)
def test_confirmation_signing_domains_reject_naive_datetimes(
    builder: Any, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="timezone"):
        builder(**kwargs)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("purpose",), "top5_confirmation"),
        (("bench_version",), 8),
        (("slot_id",), "slot-8"),
        (("settings_revision",), 0),
        (("per_bundle_request_cap",), 0),
        (("per_bundle_request_cap",), 100_001),
        (("per_bundle_token_cap",), 0),
        (("per_bundle_token_cap",), 100_000_001),
        (("deadline",), datetime(2026, 8, 9, 1, 2, 3)),
        (("execution_profile", "revision"), ""),
        (("execution_profile", "checksum"), "A" * 64),
        (("execution_profile", "schema_version"), 2),
        (("execution_profile", "longmem_projection_key_sha256"), "A" * 64),
        (("execution_profile", "provider_lanes", 0, "max_requests"), 0),
        (("execution_profile", "inference_ablation", "intervention"), "embedding"),
        (("execution_profile", "embedding_ablation", "intervention"), "inference"),
        (("execution_profile", "composite", "base_weight_bps"), 10_000),
    ],
)
def test_job_contract_rejects_wrong_purpose_version_profile_and_caps(
    path: tuple[str | int, ...], value: Any
) -> None:
    payload = copy.deepcopy(_job_payload())
    target: Any = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        V9ConfirmationJobResponse.model_validate(payload)


def test_job_contract_requires_longmem_projection_key() -> None:
    payload = _job_payload()
    del payload["execution_profile"]["longmem_projection_key_sha256"]

    with pytest.raises(ValidationError):
        V9ConfirmationJobResponse.model_validate(payload)


@pytest.mark.parametrize(
    "payload_change",
    [
        {"slot_id": "slot-8"},
        {"profile_revision": ""},
        {"profile_checksum": "A" * 64},
        {"requested_at": datetime(2026, 8, 8, 1, 2, 3)},
        {"unknown": "not allowed"},
    ],
)
def test_claim_contract_is_strict_and_fail_closed(
    payload_change: dict[str, Any],
) -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    payload: dict[str, Any] = {
        "validator_hotkey": keypair.ss58_address,
        "slot_id": "slot-3",
        "profile_revision": _PROFILE_REVISION,
        "profile_checksum": _PROFILE_CHECKSUM,
        "nonce": _NONCE,
        "requested_at": _REQUESTED_AT,
        "signature": "ab" * 64,
    }
    payload.update(payload_change)

    with pytest.raises(ValidationError):
        V9ConfirmationClaimRequest.model_validate(payload)


def test_report_and_submit_contracts_forbid_unknown_fields() -> None:
    report = _report().model_dump(mode="python")
    report["untrusted_advisory"] = True
    with pytest.raises(ValidationError):
        V9ConfirmationCompletionReport.model_validate(report)

    submit = {
        "validator_hotkey": _HOTKEY,
        "ticket_id": _TICKET_ID,
        "report": _report(),
        "legacy_confirmation_seeds": [1, 2, 3],
    }
    with pytest.raises(ValidationError):
        V9ConfirmationSubmitRequest.model_validate(submit)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("accepted", False),
        ("state", "leased"),
        ("qualification_status", "provisional"),
        ("evidence_sha256", "not-a-digest"),
    ],
)
def test_submit_response_rejects_nonterminal_or_malformed_acceptance(
    field: str, value: Any
) -> None:
    payload = _submit_response()
    payload[field] = value
    with pytest.raises(ValidationError):
        V9ConfirmationSubmitResponse.model_validate(payload)


async def test_claim_uses_exact_private_route_headers_and_signed_profile() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url == httpx.URL(
            "https://platform.test/api/v1/validator/v9-confirmation/job"
        )
        assert request.headers["X-Validator-Hotkey"] == keypair.ss58_address
        claim = V9ConfirmationClaimRequest.model_validate_json(request.content)
        assert claim.validator_hotkey == keypair.ss58_address
        assert claim.slot_id == "slot-3"
        assert claim.profile_revision == _PROFILE_REVISION
        assert claim.profile_checksum == _PROFILE_CHECKSUM
        message = v9_confirmation_claim_signing_message(
            validator_hotkey=claim.validator_hotkey,
            slot_id=claim.slot_id,
            profile_revision=claim.profile_revision,
            profile_checksum=claim.profile_checksum,
            nonce=claim.nonce,
            requested_at=claim.requested_at,
        )
        assert keypair.verify(message, bytes.fromhex(claim.signature))
        return httpx.Response(200, json=_job_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlatformClient(_config(keypair.ss58_address), http, keypair)
        job = await client.request_v9_confirmation_job(
            slot_id="slot-3",
            profile_revision=_PROFILE_REVISION,
            profile_checksum=_PROFILE_CHECKSUM,
        )

    assert job is not None
    assert job.purpose == "v9_confirmation_bundle"
    assert job.bench_version == 9
    assert len(requests) == 1
    assert "top5-confirmation" not in requests[0].url.path


async def test_claim_returns_none_on_204_without_parsing_a_body() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/validator/v9-confirmation/job")
        return httpx.Response(204, content=b"not-json-and-must-not-be-read")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlatformClient(_config(keypair.ss58_address), http, keypair)
        result = await client.request_v9_confirmation_job(
            slot_id="slot-0",
            profile_revision=_PROFILE_REVISION,
            profile_checksum=_PROFILE_CHECKSUM,
        )

    assert result is None


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda body: body.update(purpose="canonical_quorum"), "response was invalid"),
        (lambda body: body.update(bench_version=8), "response was invalid"),
        (lambda body: body.update(slot_id="slot-4"), "signed claim"),
        (
            lambda body: body["execution_profile"].update(revision="wrong-profile"),
            "signed claim",
        ),
        (
            lambda body: body["execution_profile"].update(checksum="ee" * 32),
            "signed claim",
        ),
        (lambda body: body.update(per_bundle_request_cap=0), "response was invalid"),
        (
            lambda body: body.update(per_bundle_request_cap=100_001),
            "response was invalid",
        ),
        (lambda body: body.update(per_bundle_token_cap=0), "response was invalid"),
        (
            lambda body: body.update(per_bundle_token_cap=100_000_001),
            "response was invalid",
        ),
        (
            lambda body: body.update(deadline="2026-08-09T01:02:03"),
            "response was invalid",
        ),
        (
            lambda body: body["execution_profile"].update(unexpected=True),
            "response was invalid",
        ),
    ],
)
async def test_claim_rejects_malformed_or_requested_identity_mismatched_job(
    mutate: Any, error: str
) -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")

    def handler(_: httpx.Request) -> httpx.Response:
        body = copy.deepcopy(_job_payload())
        mutate(body)
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformError, match=error):
            client = PlatformClient(_config(keypair.ss58_address), http, keypair)
            await client.request_v9_confirmation_job(
                slot_id="slot-3",
                profile_revision=_PROFILE_REVISION,
                profile_checksum=_PROFILE_CHECKSUM,
            )


async def test_claim_rejects_invalid_json_and_http_rejection() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    responses = iter(
        [
            httpx.Response(200, content=b"{"),
            httpx.Response(409, text="profile is not registered"),
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlatformClient(_config(keypair.ss58_address), http, keypair)
        with pytest.raises(PlatformError, match="job response was invalid"):
            await client.request_v9_confirmation_job(
                slot_id="slot-3",
                profile_revision=_PROFILE_REVISION,
                profile_checksum=_PROFILE_CHECKSUM,
            )
        with pytest.raises(PlatformError, match=r"claim rejected \(409\)"):
            await client.request_v9_confirmation_job(
                slot_id="slot-3",
                profile_revision=_PROFILE_REVISION,
                profile_checksum=_PROFILE_CHECKSUM,
            )


async def test_artifact_fetch_uses_bundle_route_and_ticket_bound_signature() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == httpx.URL(
            f"https://platform.test/api/v1/validator/v9-confirmation/bundle/{_BUNDLE_ID}/artifact"
        )
        assert request.headers["X-Validator-Hotkey"] == keypair.ss58_address
        assert request.headers["X-Confirmation-Ticket-Id"] == str(_TICKET_ID)
        nonce = UUID(request.headers["X-Confirmation-Nonce"])
        requested_at = datetime.fromisoformat(
            request.headers["X-Confirmation-Requested-At"]
        )
        message = v9_confirmation_artifact_signing_message(
            validator_hotkey=keypair.ss58_address,
            bundle_id=_BUNDLE_ID,
            ticket_id=_TICKET_ID,
            nonce=nonce,
            requested_at=requested_at,
        )
        assert keypair.verify(
            message,
            bytes.fromhex(request.headers["X-Confirmation-Signature"]),
        )
        assert "X-Validator-Artifact-Signature" not in request.headers
        return httpx.Response(200, json=_artifact_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        artifact = await PlatformClient(
            _config(keypair.ss58_address), http, keypair
        ).get_v9_confirmation_artifact(_job())

    assert artifact.agent_id == _AGENT_ID
    assert artifact.sha256 == _ARTIFACT_SHA
    assert artifact.bench_version == 9


@pytest.mark.parametrize(
    "identity_change",
    [
        {"agent_id": "40000000-0000-0000-0000-000000000099"},
        {"sha256": "ff" * 32},
        {"bench_version": 8},
        {"bench_version": None},
    ],
)
async def test_artifact_fetch_rejects_every_identity_mismatch(
    identity_change: dict[str, Any],
) -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")

    def handler(_: httpx.Request) -> httpx.Response:
        body = _artifact_payload()
        body.update(identity_change)
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformError, match="artifact identity mismatch"):
            await PlatformClient(
                _config(keypair.ss58_address), http, keypair
            ).get_v9_confirmation_artifact(_job())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"agent_id": str(_AGENT_ID)}),
    ],
)
async def test_artifact_fetch_normalizes_malformed_response(
    response: httpx.Response,
) -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response)
    ) as http:
        with pytest.raises(PlatformError, match="artifact response was invalid"):
            await PlatformClient(
                _config(keypair.ss58_address), http, keypair
            ).get_v9_confirmation_artifact(_job())


async def test_prepare_uses_exact_private_route_header_body_digest_and_signature() -> (
    None
):
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    result = _scorer_result()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == httpx.URL(
            f"https://platform.test/api/v1/validator/v9-confirmation/bundle/{_BUNDLE_ID}/prepare-report"
        )
        assert request.headers["X-Validator-Hotkey"] == keypair.ss58_address
        assert "top5-confirmation" not in request.url.path
        prepare = V9ConfirmationPrepareRequest.model_validate_json(request.content)
        assert prepare.validator_hotkey == keypair.ss58_address
        assert prepare.ticket_id == _TICKET_ID
        assert prepare.ablation_coordinator_latency_ms == 37
        assert prepare.longmemeval == result.longmemeval
        assert prepare.inference_ablation == result.inference_ablation
        assert prepare.embedding_ablation == result.embedding_ablation
        rebuilt = v9_confirmation_prepare_wire_sha256(
            ablation_coordinator_latency_ms=prepare.ablation_coordinator_latency_ms,
            longmemeval=prepare.longmemeval.model_dump(mode="json"),
            inference_ablation=prepare.inference_ablation.model_dump(mode="json"),
            embedding_ablation=prepare.embedding_ablation.model_dump(mode="json"),
        )
        assert prepare.wire_sha256 == rebuilt == result.evidence_sha256
        signed = v9_confirmation_prepare_signing_message(
            validator_hotkey=prepare.validator_hotkey,
            bundle_id=_BUNDLE_ID,
            ticket_id=prepare.ticket_id,
            wire_sha256=prepare.wire_sha256,
            nonce=prepare.nonce,
            requested_at=prepare.requested_at,
        )
        assert keypair.verify(signed, bytes.fromhex(prepare.signature))
        assert json.loads(request.content) == {
            "validator_hotkey": keypair.ss58_address,
            "ticket_id": str(_TICKET_ID),
            "nonce": str(prepare.nonce),
            "requested_at": prepare.requested_at.isoformat().replace("+00:00", "Z"),
            "wire_sha256": result.evidence_sha256,
            "ablation_coordinator_latency_ms": 37,
            "longmemeval": result.longmemeval.model_dump(mode="json"),
            "inference_ablation": result.inference_ablation.model_dump(mode="json"),
            "embedding_ablation": result.embedding_ablation.model_dump(mode="json"),
            "signature": prepare.signature,
        }
        return httpx.Response(200, json=_prepared_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        prepared = await PlatformClient(
            _config(keypair.ss58_address), http, keypair
        ).prepare_v9_confirmation_report(_job(), result)

    assert prepared == _prepared()
    assert prepared.evidence_sha256 == _EVIDENCE_SHA
    assert prepared.evidence_sha256 != result.evidence_sha256


async def test_prepare_rejects_untrusted_scorer_wire_digest_before_http() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    result = _scorer_result().model_copy(update={"evidence_sha256": "99" * 32})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformError, match=r"scorer .* digest"):
            await PlatformClient(
                _config(keypair.ss58_address), http, keypair
            ).prepare_v9_confirmation_report(_job(), result)

    assert requests == 0


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda body: body.update(bundle_id="10000000-0000-0000-0000-000000000099"),
            "identity mismatch",
        ),
        (
            lambda body: body.update(ticket_id="20000000-0000-0000-0000-000000000099"),
            "identity mismatch",
        ),
        (
            lambda body: body.update(ablation_coordinator_latency_ms=38),
            "identity mismatch",
        ),
        (lambda body: body.pop("evidence_sha256"), "response was invalid"),
        (
            lambda body: body.update(evidence_sha256="not-a-root"),
            "response was invalid",
        ),
        (lambda body: body.update(unexpected=True), "response was invalid"),
    ],
)
async def test_prepare_rejects_malformed_or_identity_mismatched_response(
    mutate: Any, error: str
) -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")

    def handler(_: httpx.Request) -> httpx.Response:
        body = copy.deepcopy(_prepared_payload())
        mutate(body)
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformError, match=error):
            await PlatformClient(
                _config(keypair.ss58_address), http, keypair
            ).prepare_v9_confirmation_report(_job(), _scorer_result())


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(200, content=b"{"), "response was invalid"),
        (httpx.Response(409, text="ticket expired"), r"rejected \(409\)"),
        (httpx.Response(204), r"rejected \(204\)"),
    ],
)
async def test_prepare_normalizes_invalid_json_rejection_and_no_content(
    response: httpx.Response, error: str
) -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response)
    ) as http:
        with pytest.raises(PlatformError, match=error):
            await PlatformClient(
                _config(keypair.ss58_address), http, keypair
            ).prepare_v9_confirmation_report(_job(), _scorer_result())


async def test_report_submission_uses_exact_route_header_and_body() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    report = _report()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == httpx.URL(
            f"https://platform.test/api/v1/validator/v9-confirmation/bundle/{_BUNDLE_ID}/report"
        )
        assert request.headers["X-Validator-Hotkey"] == keypair.ss58_address
        assert "top5-confirmation-score" not in request.url.path
        payload = V9ConfirmationSubmitRequest.model_validate_json(request.content)
        assert payload.validator_hotkey == keypair.ss58_address
        assert payload.ticket_id == _TICKET_ID
        assert payload.report == report
        assert json.loads(request.content) == {
            "validator_hotkey": keypair.ss58_address,
            "ticket_id": str(_TICKET_ID),
            "report": report.model_dump(mode="json"),
        }
        return httpx.Response(200, json=_submit_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        accepted = await PlatformClient(
            _config(keypair.ss58_address), http, keypair
        ).submit_v9_confirmation_report(_job(), report)

    assert accepted.accepted is True
    assert accepted.qualification_status == "qualified"
    assert accepted.replayed is False


@pytest.mark.parametrize("replayed", [False, True])
async def test_report_submission_accepts_initial_and_idempotent_replay(
    replayed: bool,
) -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=_submit_response(replayed=replayed))
        )
    ) as http:
        accepted = await PlatformClient(
            _config(keypair.ss58_address), http, keypair
        ).submit_v9_confirmation_report(_job(), _report())

    assert accepted.replayed is replayed
    assert accepted.state == "completed"


@pytest.mark.parametrize(
    "identity_change",
    [
        {"bundle_id": "10000000-0000-0000-0000-000000000099"},
        {"ticket_id": "20000000-0000-0000-0000-000000000099"},
    ],
)
async def test_report_submission_rejects_acceptance_identity_mismatch(
    identity_change: dict[str, Any],
) -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")

    def handler(_: httpx.Request) -> httpx.Response:
        body = _submit_response()
        body.update(identity_change)
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformError, match="acceptance identity mismatch"):
            await PlatformClient(
                _config(keypair.ss58_address), http, keypair
            ).submit_v9_confirmation_report(_job(), _report())


@pytest.mark.parametrize(
    "response,error",
    [
        (httpx.Response(200, content=b"{"), "submit response was invalid"),
        (httpx.Response(200, json={"accepted": True}), "submit response was invalid"),
        (httpx.Response(409, text="duplicate conflict"), r"report rejected \(409\)"),
    ],
)
async def test_report_submission_normalizes_malformed_and_rejected_responses(
    response: httpx.Response, error: str
) -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response)
    ) as http:
        with pytest.raises(PlatformError, match=error):
            await PlatformClient(
                _config(keypair.ss58_address), http, keypair
            ).submit_v9_confirmation_report(_job(), _report())


@pytest.mark.parametrize("replayed", [False, True])
async def test_failure_hand_back_uses_only_private_signed_route(replayed: bool) -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == (
            f"/api/v1/validator/v9-confirmation/bundle/{_BUNDLE_ID}/fail"
        )
        body = json.loads(request.content)
        requested_at = datetime.fromisoformat(body["requested_at"])
        signed = v9_confirmation_fail_signing_message(
            validator_hotkey=body["validator_hotkey"],
            bundle_id=_BUNDLE_ID,
            ticket_id=UUID(body["ticket_id"]),
            reason=body["reason"],
            nonce=UUID(body["nonce"]),
            requested_at=requested_at,
        )
        assert keypair.verify(signed, bytes.fromhex(body["signature"]))
        assert body["reason"] == "infrastructure"
        return httpx.Response(
            200,
            json={
                "bundle_id": str(_BUNDLE_ID),
                "ticket_id": str(_TICKET_ID),
                "accepted": True,
                "state": "failed",
                "settled_microusd": 200_000,
                "replayed": replayed,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        failed = await PlatformClient(
            _config(keypair.ss58_address), http, keypair
        ).fail_v9_confirmation_job(_job(), reason="infrastructure")

    assert failed.replayed is replayed
    assert failed.settled_microusd == 200_000


async def test_v9_flow_never_invokes_legacy_top5_or_canonical_artifact_methods() -> (
    None
):
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    response_number = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal response_number
        assert "top5-confirmation" not in request.url.path
        assert f"/agent/{_AGENT_ID}/" not in request.url.path
        response_number += 1
        if request.url.path.endswith("/job"):
            return httpx.Response(200, json=_job_payload())
        if request.url.path.endswith("/artifact"):
            return httpx.Response(200, json=_artifact_payload())
        return httpx.Response(200, json=_submit_response(replayed=True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlatformClient(_config(keypair.ss58_address), http, keypair)
        request_top5_confirmation_job = AsyncMock(
            side_effect=AssertionError("legacy claim invoked")
        )
        get_artifact = AsyncMock(
            side_effect=AssertionError("canonical artifact invoked")
        )
        submit_top5_confirmation_score = AsyncMock(
            side_effect=AssertionError("legacy submit invoked")
        )
        with (
            patch.object(
                client, "request_top5_confirmation_job", request_top5_confirmation_job
            ),
            patch.object(client, "get_artifact", get_artifact),
            patch.object(
                client,
                "submit_top5_confirmation_score",
                submit_top5_confirmation_score,
            ),
        ):
            job = await client.request_v9_confirmation_job(
                slot_id="slot-3",
                profile_revision=_PROFILE_REVISION,
                profile_checksum=_PROFILE_CHECKSUM,
            )
            assert job is not None
            await client.get_v9_confirmation_artifact(job)
            await client.submit_v9_confirmation_report(job, _report())

    assert response_number == 3
    request_top5_confirmation_job.assert_not_awaited()
    get_artifact.assert_not_awaited()
    submit_top5_confirmation_score.assert_not_awaited()


async def test_worker_independently_rebuilds_every_signed_root_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job().model_copy(
        update={
            "slot_id": "slot-0",
            "deadline": datetime.now(UTC) + timedelta(hours=1),
        }
    )
    scorer_result = _scorer_result()
    prepared, expected_root, expected_digest = _worker_prepared_report(job)
    assert scorer_result.evidence_sha256 != expected_digest
    artifact = ArtifactResponse.model_validate(_artifact_payload())

    platform = MagicMock()
    platform.request_v9_confirmation_job = AsyncMock(return_value=job)
    platform.get_v9_confirmation_artifact = AsyncMock(return_value=artifact)
    platform.prepare_v9_confirmation_report = AsyncMock(return_value=prepared)
    platform.submit_v9_confirmation_report = AsyncMock()
    platform.fail_v9_confirmation_job = AsyncMock()
    dittobench = MagicMock()
    dittobench.v9_confirmation_readiness = AsyncMock(
        return_value=V9ConfirmationScorerReadiness(
            ready=True,
            profile_revision=_PROFILE_REVISION,
            profile_checksum=_PROFILE_CHECKSUM,
        )
    )
    dittobench.execute_v9_confirmation = AsyncMock(return_value=scorer_result)
    signing_spy = MagicMock(return_value="ab" * 64)
    rebuilt: list[tuple[object, str]] = []

    def capture_rebuild(
        rebuild_job: V9ConfirmationJobResponse,
        rebuild_result: V9ConfirmationScorerResult,
        rebuild_prepared: V9ConfirmationPreparedReport,
    ) -> tuple[object, str]:
        result = rebuild_v9_confirmation_evidence_root(
            rebuild_job, rebuild_result, rebuild_prepared
        )
        rebuilt.append(result)
        return result

    monkeypatch.setattr(
        worker_module,
        "rebuild_v9_confirmation_evidence_root",
        capture_rebuild,
    )
    monkeypatch.setattr(
        worker_module,
        "sign_v9_confirmation_bundle",
        signing_spy,
    )
    config = cast(
        Any,
        SimpleNamespace(
            validator_hotkey=_HOTKEY,
            netuid=118,
            benchmark_capacity=1,
        ),
    )
    worker = ValidatorWorker(
        config=config,
        platform=platform,
        dittobench=dittobench,
        chain=MagicMock(),
        keypair=MagicMock(),
    )

    await worker._run_v9_confirmation_lane(slot_ids=["slot-0"])

    platform.prepare_v9_confirmation_report.assert_awaited_once_with(job, scorer_result)
    assert len(rebuilt) == 1
    rebuilt_root = cast(V9ConfirmationEvidenceRoot, rebuilt[0][0])
    assert rebuilt[0][1] == expected_digest
    assert rebuilt_root == expected_root
    assert len(type(rebuilt_root).model_fields) == 14
    assert rebuilt_root.schema_version == 1
    assert rebuilt_root.artifact_sha256 == job.artifact_sha256
    assert rebuilt_root.bench_version == 9
    assert rebuilt_root.confirmation_profile_revision == job.execution_profile.revision
    assert rebuilt_root.confirmation_profile_checksum == job.execution_profile.checksum
    assert rebuilt_root.settings_revision == job.settings_revision
    assert rebuilt_root.settings_checksum == job.settings_checksum
    assert rebuilt_root.retest_generation == job.retest_generation
    assert rebuilt_root.ablation_coordinator_latency_ms == 37
    assert rebuilt_root.composite_policy == expected_root.composite_policy
    assert rebuilt_root.longmemeval == prepared.longmemeval
    assert rebuilt_root.inference_ablation == prepared.inference_ablation
    assert rebuilt_root.embedding_ablation == prepared.embedding_ablation
    assert rebuilt_root.totals.model_dump() == {
        "request_count": 12,
        "input_tokens": 1_200,
        "output_tokens": 120,
        "provider_cost_microusd": 12_345,
        "latency_ms": 4_358,
    }
    signing_spy.assert_called_once()
    assert signing_spy.call_args.kwargs["evidence_sha256"] == expected_digest
    assert signing_spy.call_args.kwargs["evidence_sha256"] != (
        scorer_result.evidence_sha256
    )
    platform.submit_v9_confirmation_report.assert_awaited_once()
    submitted_job, submitted_report = (
        platform.submit_v9_confirmation_report.await_args.args
    )
    assert submitted_job == job
    assert submitted_report.bundle_signature == "ab" * 64
    assert submitted_report.ablation_coordinator_latency_ms == 37
    assert submitted_report.longmemeval == prepared.longmemeval
    assert submitted_report.inference_ablation == prepared.inference_ablation
    assert submitted_report.embedding_ablation == prepared.embedding_ablation
    assert submitted_report.longmemeval != scorer_result.longmemeval.model_dump(
        mode="json"
    )
    platform.fail_v9_confirmation_job.assert_not_awaited()


def test_rebuild_rejects_internally_consistent_platform_fabrication() -> None:
    """A self-consistent prepare response cannot replace scorer-owned evidence."""
    job = _job()
    scorer_result = _scorer_result()
    prepared, expected_root, _ = _worker_prepared_report(job)
    original_lane = prepared.longmemeval.evidence.provider_evidence[0]
    fabricated_lane = original_lane.model_copy(
        update={
            "cost_usd_micros": original_lane.cost_usd_micros + 1,
            "receipt_set_sha256": "fe" * 32,
        }
    )
    fabricated_evidence = prepared.longmemeval.evidence.model_copy(
        update={
            "provider_evidence": [
                fabricated_lane,
                *prepared.longmemeval.evidence.provider_evidence[1:],
            ]
        }
    )
    fabricated_longmem = prepared.longmemeval.model_copy(
        update={
            "evidence": fabricated_evidence,
            "evidence_sha256": evidence_digest(fabricated_evidence),
            "provider_cost_microusd": (prepared.longmemeval.provider_cost_microusd + 1),
        }
    )
    fabricated_totals = expected_root.totals.model_copy(
        update={
            "provider_cost_microusd": (expected_root.totals.provider_cost_microusd + 1)
        }
    )
    fabricated_root = expected_root.model_copy(
        update={
            "longmemeval": fabricated_longmem,
            "totals": fabricated_totals,
        }
    )
    fabricated_prepared = prepared.model_copy(
        update={
            "longmemeval": fabricated_longmem,
            "evidence_sha256": evidence_digest(fabricated_root),
        }
    )

    with pytest.raises(
        ValueError,
        match="Platform-prepared confirmation envelopes do not match native evidence",
    ):
        rebuild_v9_confirmation_evidence_root(
            job,
            scorer_result,
            fabricated_prepared,
        )


def test_rebuild_rejects_native_wire_digest_tampering() -> None:
    job = _job()
    scorer_result = _scorer_result().model_copy(update={"evidence_sha256": "ff" * 32})
    prepared, _, _ = _worker_prepared_report(job)

    with pytest.raises(ValueError, match="native wire digest mismatch"):
        rebuild_v9_confirmation_evidence_root(job, scorer_result, prepared)


def test_rebuild_rejects_native_dimension_evidence_tampering() -> None:
    job = _job()
    original = _scorer_result()
    native = original.longmemeval.model_dump(mode="json")
    native_evidence = cast(dict[str, object], native["evidence"])
    native_evidence["dataset_revision"] = "fabricated-revision"
    fabricated_dimension = V9ConfirmationRawDimension.model_validate(native)
    wire_sha256 = v9_confirmation_prepare_wire_sha256(
        ablation_coordinator_latency_ms=original.ablation_coordinator_latency_ms,
        longmemeval=fabricated_dimension.model_dump(mode="json"),
        inference_ablation=original.inference_ablation.model_dump(mode="json"),
        embedding_ablation=original.embedding_ablation.model_dump(mode="json"),
    )
    fabricated_result = original.model_copy(
        update={
            "longmemeval": fabricated_dimension,
            "evidence_sha256": wire_sha256,
        }
    )
    prepared, _, _ = _worker_prepared_report(job)

    with pytest.raises(ValueError, match="native evidence is invalid"):
        rebuild_v9_confirmation_evidence_root(job, fabricated_result, prepared)


@pytest.mark.parametrize(
    "tampered_field",
    [
        "evidence_sha256",
        "longmemeval",
        "inference_ablation",
        "embedding_ablation",
        "ablation_coordinator_latency_ms",
    ],
)
async def test_worker_rejects_platform_root_tampering_before_sign_or_submit(
    monkeypatch: pytest.MonkeyPatch,
    tampered_field: str,
) -> None:
    job = _job().model_copy(
        update={
            "slot_id": "slot-0",
            "deadline": datetime.now(UTC) + timedelta(hours=1),
        }
    )
    scorer_result = _scorer_result()
    prepared, _, _ = _worker_prepared_report(job)
    if tampered_field == "evidence_sha256":
        prepared = prepared.model_copy(update={tampered_field: "ff" * 32})
    elif tampered_field == "ablation_coordinator_latency_ms":
        prepared = prepared.model_copy(update={tampered_field: 38})
    else:
        original_envelope = getattr(prepared, tampered_field)
        assert isinstance(
            original_envelope,
            (LongMemDimensionEnvelope, AblationDimensionEnvelope),
        )
        envelope = original_envelope.model_copy(
            update={"latency_ms": original_envelope.latency_ms + 1}
        )
        prepared = prepared.model_copy(update={tampered_field: envelope})

    platform = MagicMock()
    platform.request_v9_confirmation_job = AsyncMock(return_value=job)
    platform.get_v9_confirmation_artifact = AsyncMock(
        return_value=ArtifactResponse.model_validate(_artifact_payload())
    )
    platform.prepare_v9_confirmation_report = AsyncMock(return_value=prepared)
    platform.submit_v9_confirmation_report = AsyncMock()
    platform.fail_v9_confirmation_job = AsyncMock()
    dittobench = MagicMock()
    dittobench.v9_confirmation_readiness = AsyncMock(
        return_value=V9ConfirmationScorerReadiness(
            ready=True,
            profile_revision=_PROFILE_REVISION,
            profile_checksum=_PROFILE_CHECKSUM,
        )
    )
    dittobench.execute_v9_confirmation = AsyncMock(return_value=scorer_result)
    signing_spy = MagicMock(return_value="ab" * 64)
    monkeypatch.setattr(
        worker_module,
        "sign_v9_confirmation_bundle",
        signing_spy,
    )
    worker = ValidatorWorker(
        config=cast(
            Any,
            SimpleNamespace(
                validator_hotkey=_HOTKEY,
                netuid=118,
                benchmark_capacity=1,
            ),
        ),
        platform=platform,
        dittobench=dittobench,
        chain=MagicMock(),
        keypair=MagicMock(),
    )

    await worker._run_v9_confirmation_lane(slot_ids=["slot-0"])

    signing_spy.assert_not_called()
    platform.submit_v9_confirmation_report.assert_not_awaited()
    platform.fail_v9_confirmation_job.assert_awaited_once_with(
        job, reason="execution_failed"
    )


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (DittobenchError("bad evidence"), "execution_failed"),
        (worker_module.ValidatorInfrastructureError("local outage"), "infrastructure"),
        (worker_module.LeaseDeadlineError("deadline"), "deadline"),
    ],
)
async def test_worker_hands_private_failure_back_with_typed_reason(
    error: Exception, reason: str
) -> None:
    job = _job().model_copy(
        update={
            "slot_id": "slot-0",
            "deadline": datetime.now(UTC) + timedelta(hours=1),
        }
    )
    platform = MagicMock()
    platform.request_v9_confirmation_job = AsyncMock(return_value=job)
    platform.get_v9_confirmation_artifact = AsyncMock(
        return_value=ArtifactResponse.model_validate(_artifact_payload())
    )
    platform.fail_v9_confirmation_job = AsyncMock()
    platform.submit_v9_confirmation_report = AsyncMock()
    dittobench = MagicMock()
    dittobench.v9_confirmation_readiness = AsyncMock(
        return_value=V9ConfirmationScorerReadiness(
            ready=True,
            profile_revision=_PROFILE_REVISION,
            profile_checksum=_PROFILE_CHECKSUM,
        )
    )
    dittobench.execute_v9_confirmation = AsyncMock(side_effect=error)
    worker = ValidatorWorker(
        config=cast(
            Any,
            SimpleNamespace(
                validator_hotkey=_HOTKEY,
                netuid=118,
                benchmark_capacity=1,
            ),
        ),
        platform=platform,
        dittobench=dittobench,
        chain=MagicMock(),
        keypair=MagicMock(),
    )

    await worker._run_v9_confirmation_lane(slot_ids=["slot-0"])

    platform.fail_v9_confirmation_job.assert_awaited_once_with(job, reason=reason)
    platform.submit_v9_confirmation_report.assert_not_awaited()


async def test_worker_cancellation_hands_back_then_reraises() -> None:
    job = _job().model_copy(
        update={
            "slot_id": "slot-0",
            "deadline": datetime.now(UTC) + timedelta(hours=1),
        }
    )
    execution_started = worker_module.asyncio.Event()

    async def never_finishes(**_kwargs: object) -> V9ConfirmationScorerResult:
        execution_started.set()
        await worker_module.asyncio.Event().wait()
        raise AssertionError("unreachable")

    platform = MagicMock()
    platform.request_v9_confirmation_job = AsyncMock(return_value=job)
    platform.get_v9_confirmation_artifact = AsyncMock(
        return_value=ArtifactResponse.model_validate(_artifact_payload())
    )
    platform.fail_v9_confirmation_job = AsyncMock()
    dittobench = MagicMock()
    dittobench.v9_confirmation_readiness = AsyncMock(
        return_value=V9ConfirmationScorerReadiness(
            ready=True,
            profile_revision=_PROFILE_REVISION,
            profile_checksum=_PROFILE_CHECKSUM,
        )
    )
    dittobench.execute_v9_confirmation = AsyncMock(side_effect=never_finishes)
    worker = ValidatorWorker(
        config=cast(
            Any,
            SimpleNamespace(
                validator_hotkey=_HOTKEY,
                netuid=118,
                benchmark_capacity=1,
            ),
        ),
        platform=platform,
        dittobench=dittobench,
        chain=MagicMock(),
        keypair=MagicMock(),
    )
    task = worker_module.asyncio.create_task(
        worker._run_v9_confirmation_lane(slot_ids=["slot-0"])
    )
    await execution_started.wait()
    task.cancel()

    with pytest.raises(worker_module.asyncio.CancelledError):
        await task

    platform.fail_v9_confirmation_job.assert_awaited_once_with(job, reason="cancelled")
