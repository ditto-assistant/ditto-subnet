"""Immutable validator/Pylon commit provenance; never itself a payout receipt."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .treasury_enforcement import EnforcingTreasuryPin

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
BlockHash = Annotated[str, Field(pattern=r"^0x[0-9a-f]{64}$")]
Counter = Annotated[int, Field(ge=0, strict=True)]
Hotkey = Annotated[str, Field(min_length=1, max_length=128)]
Weight = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


class WeightProvenance(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ledger_snapshot_id: UUID
    epoch_index: Counter
    ledger_digest: Digest
    champion_agent_id: UUID | None
    champion_artifact_sha256: Digest | None
    bench_version: Annotated[int, Field(ge=1, strict=True)]
    vector_digest: Digest

    @model_validator(mode="after")
    def paired_champion(self) -> WeightProvenance:
        if (self.champion_agent_id is None) != (self.champion_artifact_sha256 is None):
            raise ValueError("champion identity and artifact must be paired")
        return self


class FinalizedWeightAttempt(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    attempt_id: UUID
    normalized_weights: Annotated[
        list[
            tuple[
                Annotated[int, Field(ge=0, le=65535, strict=True)],
                Annotated[int, Field(ge=0, le=65535, strict=True)],
            ]
        ],
        Field(min_length=1, max_length=4096),
    ]
    ciphertext_hash: Digest
    ciphertext_hex: Annotated[
        str,
        Field(min_length=2, max_length=2 * 1024 * 1024, pattern=r"^(?:[0-9a-f]{2})+$"),
    ]
    reveal_round: Counter
    version_key: Counter
    commit_block: Annotated[int, Field(ge=1, strict=True)]
    commit_block_hash: BlockHash
    extrinsic_hash: BlockHash
    extrinsic_index: Counter

    @model_validator(mode="after")
    def validate_attempt(self) -> FinalizedWeightAttempt:
        uids = [uid for uid, _ in self.normalized_weights]
        if len(set(uids)) != len(uids):
            raise ValueError("normalized weights contain duplicate UIDs")
        if not any(value > 0 for _, value in self.normalized_weights):
            raise ValueError("normalized weights have no positive value")
        digest = hashlib.blake2b(
            bytes.fromhex(self.ciphertext_hex), digest_size=32
        ).hexdigest()
        if digest != self.ciphertext_hash:
            raise ValueError("ciphertext hash does not match ciphertext")
        return self


class FinalizedWeightReceipt(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: Literal[1, 2] = 1
    request_id: UUID
    request_digest: Digest
    task_id: Annotated[int, Field(ge=1, strict=True)]
    netuid: Annotated[int, Field(ge=0, le=65535, strict=True)]
    mechanism_id: Literal[0] = 0
    validator_hotkey: Hotkey
    weights: Annotated[dict[Hotkey, Weight], Field(min_length=1, max_length=4096)]
    provenance: WeightProvenance
    attempt: FinalizedWeightAttempt
    treasury_pin: EnforcingTreasuryPin | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("weight receipt version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_digests(self) -> FinalizedWeightReceipt:
        if self.schema_version == 1:
            if (
                self.treasury_pin is not None
                or self.provenance.champion_agent_id is None
            ):
                raise ValueError(
                    "legacy receipt requires champion and no enforcing pin"
                )
        elif self.treasury_pin is None or (
            self.treasury_pin.epoch_index != self.provenance.epoch_index
            or self.treasury_pin.policy.netuid != self.netuid
            or self.attempt.commit_block <= self.treasury_pin.pinned_block
            or not any(
                m.validator_hotkey == self.validator_hotkey
                for m in self.treasury_pin.fleet
            )
        ):
            raise ValueError("enforcing receipt differs from pinned treasury authority")
        if self.treasury_pin is not None and (
            not math.isclose(
                math.fsum(self.weights.values()), 1, abs_tol=1e-12, rel_tol=0
            )
            or not math.isclose(
                self.weights.get(self.treasury_pin.policy.collector_hotkey, 0),
                self.treasury_pin.policy.service_bps / 10_000,
                abs_tol=1e-12,
                rel_tol=0,
            )
        ):
            raise ValueError("enforcing receipt does not conserve service allocation")
        if weight_vector_digest(self.weights) != self.provenance.vector_digest:
            raise ValueError("weight vector digest does not match request weights")
        if weight_request_digest(self) != self.request_digest:
            raise ValueError("Pylon request digest does not match immutable request")
        return self


def weight_vector_digest(weights: dict[str, float]) -> str:
    """Receipt protocol digest; unlike legacy v27, hashes the weights object."""
    return hashlib.sha256(
        _canonical({k: float(v) for k, v in weights.items()})
    ).hexdigest()


def weight_request_digest(receipt: FinalizedWeightReceipt) -> str:
    body = {
        "schema_version": receipt.schema_version,
        "mechanism_id": receipt.mechanism_id,
        "weights": {k: float(v) for k, v in receipt.weights.items()},
        "provenance": receipt.provenance.model_dump(mode="json"),
    }
    if receipt.treasury_pin is not None:
        body["treasury_pin"] = receipt.treasury_pin.model_dump(mode="json")
    return hashlib.sha256(_canonical(body)).hexdigest()


def weight_receipt_digest(receipt: FinalizedWeightReceipt) -> str:
    return hashlib.sha256(_canonical(receipt.model_dump(mode="json"))).hexdigest()


def weight_receipt_signing_message(
    receipt: FinalizedWeightReceipt, timestamp: int
) -> bytes:
    """Bind every known field and authenticated hotkey/subnet in a versioned domain."""
    return (
        f"ditto-validator-weight-receipt:v{receipt.schema_version}:".encode()
        + _canonical(
            {
                "receipt": receipt.model_dump(mode="json"),
                "timestamp": timestamp,
            }
        )
    )


class SubmitWeightReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    receipt: FinalizedWeightReceipt
    timestamp: Counter
    signature: Annotated[str, Field(pattern=r"^(0x)?[0-9a-fA-F]{128}$")]


class SubmitWeightReceiptResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    request_id: UUID
    attempt_id: UUID
    receipt_digest: Digest
    stored: Literal[True] = True
