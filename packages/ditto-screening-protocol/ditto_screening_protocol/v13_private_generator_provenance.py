"""Default-off verifier for sealed V13 group-generator provenance.

The isolated generator must persist a signed, append-only record after it has
written a manifest and before Platform registers that role. This verifier does
not accept a caller-supplied projection as proof: it checks the signature and
trusted ledger time, then independently re-derives the entire ordered case
inventory from the committed seed bytes and protected blueprint bank.

This module has no HTTP route, private bank, ledger, or production key wiring.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections import defaultdict
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto_screening_protocol.v13_private_clean_control import TrustedGenerationRegistry
from ditto_screening_protocol.v13_private_package import (
    V13_PRIVATE_PROFILE,
    V13PrivateManifest,
    V13PrivatePair,
)
from ditto_screening_protocol.v13_private_provision import (
    PrivateBlueprintPair,
    ProtectedBlueprintBank,
)
from ditto_screening_protocol.v13_private_seed import (
    AuthenticatedGeneratorDerivationReceipt,
    SealedSeedRecord,
    SeedGenerationGroup,
    StoredSeedBundle,
    _ordered_payload_digests_sha256,
    _seed_commitment,
)

_SHA_PATTERN = r"^[0-9a-f]{64}$"
_MAX_RECORD_BYTES = 2048
_MAX_BUNDLE_BYTES = 2048
_MAX_MANIFEST_BYTES = 256 * 1024
_SIGNATURE_DOMAIN = b"ditto-v13-private-generator-attestation-v1\0"
_REQUIRED_CLASSES = (
    "field_entity_rename",
    "request_paraphrase",
    "record_reorder_decoy",
)
Role = Literal["target", "known_benign"]


class SignedGeneratorAttestation(BaseModel):
    """Exact signed body; the signature never authenticates miner content."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: Literal["v13-private-generator-attestation-v1"]
    group_id: UUID
    role: Role
    sealed_bundle_sha256: str = Field(pattern=_SHA_PATTERN)
    blueprint_bank_sha256: str = Field(pattern=_SHA_PATTERN)
    blueprint_approval_receipt_sha256: str = Field(pattern=_SHA_PATTERN)
    generator_revision: Literal["v13-private-case-generator-v1"]
    manifest_sha256: str = Field(pattern=_SHA_PATTERN)
    ordered_payload_digests_sha256: str = Field(pattern=_SHA_PATTERN)
    generated_at: datetime
    signature_sha256: str = Field(pattern=_SHA_PATTERN)


class CommittedGeneratorAttestation(BaseModel):
    """Ledger-supplied bytes and time, never a generator-claimed timestamp."""

    model_config = ConfigDict(extra="ignore", frozen=True, repr=False)

    signed_bytes: bytes = Field(repr=False)
    committed_at: datetime


class AppendOnlyGeneratorLedger(Protocol):
    """Private, unique (group, role) ledger with an authoritative commit clock."""

    async def read(
        self, group_id: UUID, role: Role
    ) -> CommittedGeneratorAttestation: ...


class SealedSeedReader(Protocol):
    async def read(self, group_id: UUID) -> SealedSeedRecord: ...


class SealedManifestReader(Protocol):
    async def read_manifest(self, sha256: str) -> bytes: ...


class ApprovedBlueprintBank(BaseModel):
    """Projection of a separate authenticated, immutable semantic review.

    The authority adapter must verify the signed reviewer provenance and the
    reviewed bank's exact immutable bytes before returning this projection.
    Neither a generator signature nor a matching bank digest is an approval.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    bank_sha256: str = Field(pattern=_SHA_PATTERN)
    approval_receipt_sha256: str = Field(pattern=_SHA_PATTERN)
    status: Literal["independently_approved"]
    approved_at: datetime


class TrustedBlueprintBankApprovalRegistry(Protocol):
    async def get_verified_approval(
        self, bank_sha256: str
    ) -> ApprovedBlueprintBank: ...


class GeneratorProvenanceUnavailable(ValueError):
    """Private derivation proof is absent or invalid; never a miner finding."""


def _canonical_body(record: SignedGeneratorAttestation) -> bytes:
    body = record.model_dump(mode="json", exclude={"signature_sha256"})
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _blueprint_bank_sha256(blueprints: tuple[PrivateBlueprintPair, ...]) -> str:
    """Pin the reviewed bank identity without serializing hidden payloads."""
    identities = sorted(
        (
            str(item.pair_id),
            item.transformation_class,
            hashlib.sha256(item.control).hexdigest(),
            hashlib.sha256(item.variant).hexdigest(),
        )
        for item in blueprints
    )
    return hashlib.sha256(
        json.dumps(identities, separators=(",", ":")).encode()
    ).hexdigest()


def _derived_pairs(
    *,
    group_id: UUID,
    seeds: tuple[bytes, bytes],
    blueprints: tuple[PrivateBlueprintPair, ...],
    tool_catalog_applicable: bool,
) -> tuple[V13PrivatePair, ...]:
    """Independently replay the pinned generator's seed-dependent selection."""
    required = _REQUIRED_CLASSES + (
        ("catalog_reorder_alias",) if tool_catalog_applicable else ()
    )
    by_class: dict[str, list[PrivateBlueprintPair]] = defaultdict(list)
    seen_ids: set[UUID] = set()
    for item in blueprints:
        if item.pair_id in seen_ids:
            raise GeneratorProvenanceUnavailable(
                "private generator provenance unavailable"
            )
        seen_ids.add(item.pair_id)
        by_class[item.transformation_class].append(item)
    needed = 2 * V13_PRIVATE_PROFILE.pairs_per_class_per_seed
    if any(len(by_class[class_name]) < needed for class_name in required):
        raise GeneratorProvenanceUnavailable("private generator provenance unavailable")

    used: set[UUID] = set()
    pairs: list[V13PrivatePair] = []
    for seed in seeds:
        commitment = _seed_commitment(group_id, seed)
        for class_name in required:
            candidates = sorted(
                by_class[class_name],
                key=lambda item: hmac.digest(seed, item.pair_id.bytes, "sha256"),
            )
            selected = [item for item in candidates if item.pair_id not in used][
                : V13_PRIVATE_PROFILE.pairs_per_class_per_seed
            ]
            if len(selected) != V13_PRIVATE_PROFILE.pairs_per_class_per_seed:
                raise GeneratorProvenanceUnavailable(
                    "private generator provenance unavailable"
                )
            for item in selected:
                used.add(item.pair_id)
                pairs.append(
                    V13PrivatePair(
                        pair_id=item.pair_id,
                        seed_commitment=commitment,
                        transformation_class=item.transformation_class,
                        control_sha256=hashlib.sha256(item.control).hexdigest(),
                        variant_sha256=hashlib.sha256(item.variant).hexdigest(),
                    )
                )
    return tuple(pairs)


class ReplayedPrivateGeneratorProvenance:
    """Private verifier implementing ``TrustedPrivateGeneratorProvenance``.

    The HMAC key must exist only in the isolated generator/verifier trust
    boundary; the ledger must be append-only and DB-time its commit. Neither
    is provided by this package. Missing dependencies fail closed.
    """

    def __init__(
        self,
        *,
        signing_key: bytes,
        ledger: AppendOnlyGeneratorLedger,
        seed_store: SealedSeedReader,
        package_store: SealedManifestReader,
        bank: ProtectedBlueprintBank,
        bank_approvals: TrustedBlueprintBankApprovalRegistry,
        generations: TrustedGenerationRegistry,
    ) -> None:
        if len(signing_key) < 32:
            raise GeneratorProvenanceUnavailable("private generator key unavailable")
        self._key = signing_key
        self._ledger = ledger
        self._seed_store = seed_store
        self._packages = package_store
        self._bank = bank
        self._bank_approvals = bank_approvals
        self._generations = generations

    async def get_verified_derivation(
        self, group_id: UUID, role: Role
    ) -> AuthenticatedGeneratorDerivationReceipt:
        try:
            async with asyncio.timeout(30):
                return await self._verify(group_id, role)
        except GeneratorProvenanceUnavailable:
            raise
        except Exception:
            raise GeneratorProvenanceUnavailable(
                "private generator provenance unavailable"
            ) from None

    async def _verify(
        self, group_id: UUID, role: Role
    ) -> AuthenticatedGeneratorDerivationReceipt:
        committed = await self._ledger.read(group_id, role)
        if (
            committed.committed_at.tzinfo is None
            or not committed.signed_bytes
            or len(committed.signed_bytes) > _MAX_RECORD_BYTES
        ):
            raise GeneratorProvenanceUnavailable(
                "private generator provenance unavailable"
            )
        raw = json.loads(committed.signed_bytes)
        if not isinstance(raw, dict) or set(raw) != set(
            SignedGeneratorAttestation.model_fields
        ):
            raise GeneratorProvenanceUnavailable(
                "private generator provenance unavailable"
            )
        record = SignedGeneratorAttestation.model_validate(raw)
        expected = hmac.new(
            self._key, _SIGNATURE_DOMAIN + _canonical_body(record), hashlib.sha256
        ).hexdigest()
        if (
            not hmac.compare_digest(record.signature_sha256, expected)
            or record.group_id != group_id
            or record.role != role
            or record.generated_at.tzinfo is None
            or not record.generated_at <= committed.committed_at
        ):
            raise GeneratorProvenanceUnavailable(
                "private generator provenance unavailable"
            )

        group = await self._generations.get_verified_group(group_id)
        if not isinstance(group, SeedGenerationGroup) or group.group_id != group_id:
            raise GeneratorProvenanceUnavailable(
                "private generator provenance unavailable"
            )
        sealed = await self._seed_store.read(group_id)
        if (
            sealed.committed_at.tzinfo is None
            or not sealed.sealed_bytes
            or len(sealed.sealed_bytes) > _MAX_BUNDLE_BYTES
            or hashlib.sha256(sealed.sealed_bytes).hexdigest()
            != record.sealed_bundle_sha256
        ):
            raise GeneratorProvenanceUnavailable(
                "private generator provenance unavailable"
            )
        bundle_raw = json.loads(sealed.sealed_bytes)
        if not isinstance(bundle_raw, dict) or set(bundle_raw) != set(
            StoredSeedBundle.model_fields
        ):
            raise GeneratorProvenanceUnavailable(
                "private generator provenance unavailable"
            )
        bundle = StoredSeedBundle.model_validate(bundle_raw)
        seeds = tuple(bytes.fromhex(value) for value in bundle.seeds_hex)
        if (
            bundle.group_id != group_id
            or bundle.profile_sha256 != group.profile_sha256
            or bundle.target_receipt_sha256 != group.target_receipt_sha256
            or bundle.control_receipt_sha256 != group.control_receipt_sha256
            or bundle.approval_receipt_sha256 != group.approval_receipt_sha256
            or len(seeds) != 2
            or any(len(seed) != 32 for seed in seeds)
            or seeds[0] == seeds[1]
            or not group.started_at < sealed.committed_at < record.generated_at
        ):
            raise GeneratorProvenanceUnavailable(
                "private generator provenance unavailable"
            )

        raw_manifest = await self._packages.read_manifest(record.manifest_sha256)
        if (
            not raw_manifest
            or len(raw_manifest) > _MAX_MANIFEST_BYTES
            or hashlib.sha256(raw_manifest).hexdigest() != record.manifest_sha256
        ):
            raise GeneratorProvenanceUnavailable(
                "private generator provenance unavailable"
            )
        manifest = V13PrivateManifest.model_validate_json(raw_manifest)
        if role == "target":
            identity = (
                group.target_agent_id,
                group.target_attempt_id,
                group.target_artifact_sha256,
                group.target_image_sha256,
            )
        else:
            identity = (
                group.control_agent_id,
                group.control_attempt_id,
                group.control_artifact_sha256,
                group.control_image_sha256,
            )
        if (
            (
                manifest.agent_id,
                manifest.attempt_id,
                manifest.artifact_sha256,
                manifest.image_sha256,
            )
            != identity
            or manifest.profile_sha256 != group.profile_sha256
            or manifest.generated_at != record.generated_at
            or _ordered_payload_digests_sha256(manifest)
            != record.ordered_payload_digests_sha256
        ):
            raise GeneratorProvenanceUnavailable(
                "private generator provenance unavailable"
            )

        blueprints = await self._bank.load()
        actual_bank_sha256 = _blueprint_bank_sha256(blueprints)
        approved_bank = await self._bank_approvals.get_verified_approval(
            actual_bank_sha256
        )
        if not isinstance(approved_bank, ApprovedBlueprintBank):
            raise GeneratorProvenanceUnavailable(
                "private generator provenance unavailable"
            )
        approved_bank = ApprovedBlueprintBank.model_validate(
            approved_bank.model_dump(mode="python")
        )
        if (
            actual_bank_sha256 != record.blueprint_bank_sha256
            or approved_bank.bank_sha256 != actual_bank_sha256
            or approved_bank.approval_receipt_sha256
            != record.blueprint_approval_receipt_sha256
            or approved_bank.status != "independently_approved"
            or approved_bank.approved_at.tzinfo is None
            or approved_bank.approved_at >= group.started_at
        ):
            raise GeneratorProvenanceUnavailable(
                "private generator provenance unavailable"
            )
        expected_pairs = _derived_pairs(
            group_id=group_id,
            seeds=(seeds[0], seeds[1]),
            blueprints=blueprints,
            tool_catalog_applicable=manifest.tool_catalog_applicable,
        )
        if manifest.pairs != expected_pairs:
            raise GeneratorProvenanceUnavailable(
                "private generator provenance unavailable"
            )
        return AuthenticatedGeneratorDerivationReceipt(
            group_id=group_id,
            role=role,
            sealed_bundle_sha256=record.sealed_bundle_sha256,
            generator_revision=record.generator_revision,
            manifest_sha256=record.manifest_sha256,
            ordered_payload_digests_sha256=record.ordered_payload_digests_sha256,
            generated_at=record.generated_at,
            attested_at=committed.committed_at,
            derivation_attestation_sha256=hashlib.sha256(
                committed.signed_bytes
            ).hexdigest(),
        )
