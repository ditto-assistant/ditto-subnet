"""Private-package preflight checks never execute or disclose challenges."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import uuid4

import pytest

from ditto_screening_protocol.v13_private_package import (
    V13_PRIVATE_PROFILE_SHA256,
    ArtifactCommitment,
    PreparedV13PrivatePackage,
    PrivatePackageRegistration,
    PrivatePackageUnavailable,
    V13PrivateManifest,
    V13PrivatePair,
    prepare_sealed_v13_package,
)


def _prepare(
    *,
    store: MemorySealedStore,
    commitment: ArtifactCommitment,
    registry: MemoryPrivateRegistry,
) -> PreparedV13PrivatePackage:
    return asyncio.run(
        prepare_sealed_v13_package(
            store=store, commitment=commitment, registry=registry
        )
    )


class MemorySealedStore:
    def __init__(self, manifest: bytes, payloads: dict[str, bytes]) -> None:
        self.manifest = manifest
        self.payloads = payloads

    async def read_manifest(self, _sha256: str) -> bytes:
        return self.manifest

    async def read_payload(self, sha256: str) -> bytes:
        return self.payloads[sha256]


class MemoryPrivateRegistry:
    def __init__(self, registration: PrivatePackageRegistration) -> None:
        self.registration = registration

    async def get_registration(
        self, _agent_id: object, _attempt_id: object
    ) -> PrivatePackageRegistration:
        return self.registration


def _package(
    *, tool_catalog_applicable: bool = False
) -> tuple[ArtifactCommitment, PrivatePackageRegistration, MemorySealedStore]:
    committed_at = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    commitment = ArtifactCommitment(
        agent_id=uuid4(),
        attempt_id=uuid4(),
        artifact_sha256="a" * 64,
        image_sha256="b" * 64,
        committed_at=committed_at,
    )
    payloads: dict[str, bytes] = {}
    pairs: list[V13PrivatePair] = []
    classes = (
        "field_entity_rename",
        "request_paraphrase",
        "record_reorder_decoy",
    ) + (("catalog_reorder_alias",) if tool_catalog_applicable else ())
    for seed in ("c" * 64, "d" * 64):
        for class_name in classes:
            for index in range(10):
                digests: list[str] = []
                for side in ("control", "variant"):
                    content = f"sealed-{seed}-{class_name}-{index}-{side}".encode()
                    digest = hashlib.sha256(content).hexdigest()
                    payloads[digest] = content
                    digests.append(digest)
                pairs.append(
                    V13PrivatePair(
                        pair_id=uuid4(),
                        seed_commitment=seed,
                        transformation_class=cast(
                            Literal[
                                "field_entity_rename",
                                "request_paraphrase",
                                "record_reorder_decoy",
                                "catalog_reorder_alias",
                            ],
                            class_name,
                        ),
                        control_sha256=digests[0],
                        variant_sha256=digests[1],
                    )
                )
    manifest = V13PrivateManifest(
        agent_id=commitment.agent_id,
        attempt_id=commitment.attempt_id,
        artifact_sha256=commitment.artifact_sha256,
        image_sha256=commitment.image_sha256,
        profile_sha256=V13_PRIVATE_PROFILE_SHA256,
        generated_at=committed_at + timedelta(minutes=1),
        tool_catalog_applicable=tool_catalog_applicable,
        pairs=tuple(pairs),
    )
    raw = json.dumps(
        manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    registration = PrivatePackageRegistration(
        agent_id=commitment.agent_id,
        attempt_id=commitment.attempt_id,
        artifact_sha256=commitment.artifact_sha256,
        image_sha256=commitment.image_sha256,
        profile_sha256=V13_PRIVATE_PROFILE_SHA256,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        registered_at=committed_at + timedelta(minutes=2),
        registrar_id="trusted-private-registry",
    )
    return commitment, registration, MemorySealedStore(raw, payloads)


@pytest.mark.parametrize("tool_catalog_applicable", [False, True])
def test_sealed_package_requires_all_classes_per_hidden_seed(
    tool_catalog_applicable: bool,
) -> None:
    commitment, registration, store = _package(
        tool_catalog_applicable=tool_catalog_applicable
    )
    prepared = _prepare(
        store=store,
        commitment=commitment,
        registry=MemoryPrivateRegistry(registration),
    )
    assert prepared.commitment == commitment
    assert prepared.registration == registration
    assert isinstance(prepared.manifest.pairs, tuple)
    assert len(prepared.manifest.pairs) == (80 if tool_catalog_applicable else 60)
    assert len({pair.seed_commitment for pair in prepared.manifest.pairs}) == 2


def test_wrong_artifact_or_precommit_registration_fails_closed() -> None:
    commitment, registration, store = _package()
    for bad_registration in (
        registration.model_copy(update={"artifact_sha256": "f" * 64}),
        registration.model_copy(update={"registered_at": commitment.committed_at}),
    ):
        with pytest.raises(PrivatePackageUnavailable, match="registration identity"):
            _prepare(
                store=store,
                commitment=commitment,
                registry=MemoryPrivateRegistry(bad_registration),
            )


def test_tampered_manifest_and_payload_fail_without_private_content() -> None:
    commitment, registration, store = _package()
    first_digest = next(iter(store.payloads))
    private_value = store.payloads[first_digest].decode()
    store.payloads[first_digest] = b"tampered hidden challenge"
    with pytest.raises(PrivatePackageUnavailable) as error:
        _prepare(
            store=store,
            commitment=commitment,
            registry=MemoryPrivateRegistry(registration),
        )
    assert private_value not in str(error.value)
    assert "tampered hidden challenge" not in str(error.value)
    store.manifest += b" "
    with pytest.raises(PrivatePackageUnavailable, match="manifest commitment"):
        _prepare(
            store=store,
            commitment=commitment,
            registry=MemoryPrivateRegistry(registration),
        )


def test_manifest_requires_predeclared_class_coverage_and_no_raw_fields() -> None:
    commitment, registration, store = _package()
    manifest = json.loads(store.manifest)
    manifest["pairs"] = manifest["pairs"][:-1]
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    store.manifest = raw
    registration = registration.model_copy(
        update={"manifest_sha256": hashlib.sha256(raw).hexdigest()}
    )
    with pytest.raises(PrivatePackageUnavailable, match="manifest unavailable"):
        _prepare(
            store=store,
            commitment=commitment,
            registry=MemoryPrivateRegistry(registration),
        )
    manifest["pairs"].append(manifest["pairs"][0])
    manifest["pairs"][0]["expected_output"] = "private answer must not appear here"
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    store.manifest = raw
    registration = registration.model_copy(
        update={"manifest_sha256": hashlib.sha256(raw).hexdigest()}
    )
    with pytest.raises(PrivatePackageUnavailable, match="private pair shape"):
        _prepare(
            store=store,
            commitment=commitment,
            registry=MemoryPrivateRegistry(registration),
        )


def test_catalog_applicability_requires_fourth_class_on_both_seeds() -> None:
    commitment, registration, store = _package(tool_catalog_applicable=True)
    manifest = json.loads(store.manifest)
    manifest["pairs"] = [
        pair
        for pair in manifest["pairs"]
        if not (
            pair["transformation_class"] == "catalog_reorder_alias"
            and pair["seed_commitment"] == "c" * 64
        )
    ]
    store.manifest = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    registration = registration.model_copy(
        update={"manifest_sha256": hashlib.sha256(store.manifest).hexdigest()}
    )
    with pytest.raises(PrivatePackageUnavailable, match="manifest unavailable"):
        _prepare(
            store=store,
            commitment=commitment,
            registry=MemoryPrivateRegistry(registration),
        )


def test_wrong_image_and_precommit_manifest_fail_closed() -> None:
    commitment, registration, store = _package()
    wrong_image = registration.model_copy(update={"image_sha256": "e" * 64})
    with pytest.raises(PrivatePackageUnavailable, match="registration identity"):
        _prepare(
            store=store,
            commitment=commitment,
            registry=MemoryPrivateRegistry(wrong_image),
        )
    manifest = json.loads(store.manifest)
    manifest["generated_at"] = (
        commitment.committed_at - timedelta(seconds=1)
    ).isoformat()
    store.manifest = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    registration = registration.model_copy(
        update={"manifest_sha256": hashlib.sha256(store.manifest).hexdigest()}
    )
    with pytest.raises(PrivatePackageUnavailable, match="identity or order"):
        _prepare(
            store=store,
            commitment=commitment,
            registry=MemoryPrivateRegistry(registration),
        )


def test_oversized_sealed_payload_fails_before_execution() -> None:
    commitment, registration, store = _package()
    payload = b"hidden" * 25_000
    digest = hashlib.sha256(payload).hexdigest()
    manifest = json.loads(store.manifest)
    manifest["pairs"][0]["control_sha256"] = digest
    store.payloads[digest] = payload
    store.manifest = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    registration = registration.model_copy(
        update={"manifest_sha256": hashlib.sha256(store.manifest).hexdigest()}
    )
    with pytest.raises(PrivatePackageUnavailable, match="payload commitment"):
        _prepare(
            store=store,
            commitment=commitment,
            registry=MemoryPrivateRegistry(registration),
        )
