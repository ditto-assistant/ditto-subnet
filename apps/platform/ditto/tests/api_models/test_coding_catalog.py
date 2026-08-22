"""Contract tests for signed coding catalog commitments."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import bittensor
import pytest
from pydantic import ValidationError

from ditto.api_models.coding_catalog import (
    CodingCatalogCommitment,
    CodingCatalogTaskExposure,
    coding_catalog_commitment_digest,
    coding_catalog_commitment_signing_message,
)

_CURATOR = bittensor.Keypair.create_from_uri("//Alice")


def _vectors() -> dict:
    return json.loads(
        (
            Path(__file__).parents[5]
            / "packages"
            / "dittobench-coding-contract"
            / "testdata"
            / "coding_catalog_v1.json"
        ).read_text(encoding="utf-8")
    )


def _commitment(**overrides: object) -> CodingCatalogCommitment:
    values: dict[str, object] = {
        "schema": "dittobench-coding-catalog-commitment-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "corpus_release_id": "private-coding-corpus-v1",
        "catalog_merkle_root": "11" * 32,
        "selection_derivation_id": "coding-selection-v1",
        "selection_chain_genesis_hash": "0x" + "22" * 32,
        "grader_contract_sha256": "33" * 32,
        "inference_grant_sha256": "44" * 32,
        "task_version_count": 100,
        "curator_hotkey": _CURATOR.ss58_address,
        "committed_at_unix": 1_787_310_000,
    }
    values.update(overrides)
    body = (
        json.dumps(
            values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
    values["commitment_sha256"] = hashlib.sha256(body).hexdigest()
    return CodingCatalogCommitment.model_validate(values)


def test_commitment_digest_unknown_fields_and_signature_domain_are_stable() -> None:
    commitment = _commitment()
    assert coding_catalog_commitment_digest(commitment) == commitment.commitment_sha256
    extended = commitment.model_dump(mode="json", by_alias=True)
    extended["future_hint"] = "ignored"
    assert coding_catalog_commitment_digest(
        CodingCatalogCommitment.model_validate(extended)
    ) == coding_catalog_commitment_digest(commitment)
    message = coding_catalog_commitment_signing_message(commitment)
    assert (
        message
        == "\x00".join(
            (
                "dittobench-coding-catalog-commitment:v1",
                _CURATOR.ss58_address,
                "private-coding-corpus-v1",
                "1",
                datetime.fromtimestamp(1_787_310_000, UTC).isoformat(
                    timespec="microseconds"
                ),
                commitment.commitment_sha256,
            )
        ).encode()
    )
    assert _CURATOR.verify(message, _CURATOR.sign(message))


def test_platform_catalog_contract_matches_public_vector() -> None:
    vector = _vectors()
    commitment = CodingCatalogCommitment.model_validate(vector["commitment"])
    exposure = CodingCatalogTaskExposure.model_validate(vector["exposure"])
    assert (
        coding_catalog_commitment_digest(commitment)
        == (vector["commitment"]["commitment_sha256"])
    )
    assert (
        hashlib.sha256(
            coding_catalog_commitment_signing_message(commitment)
        ).hexdigest()
        == vector["signing_message_sha256"]
    )
    assert exposure.task_version_id == "private-task-v1"


def test_commitment_is_permanently_shadow_and_digest_bound() -> None:
    with pytest.raises(ValidationError):
        _commitment(weight_eligible=True)
    changed = _commitment().model_dump(mode="json", by_alias=True)
    changed["catalog_merkle_root"] = "ee" * 32
    with pytest.raises(ValidationError, match="commitment_sha256"):
        CodingCatalogCommitment.model_validate(changed)
