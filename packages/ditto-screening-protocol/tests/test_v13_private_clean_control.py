"""Synthetic same-pair clean-control preflight; no hidden bank or live image."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

import pytest

from ditto_screening_protocol.v13_private_clean_control import (
    KnownBenignControlApproval,
    MatchedCleanControlUnavailable,
    TrustedGenerationGroup,
    V13MatchedCleanControlCommitment,
    compute_v13_generation_role_digest,
    prepare_v13_matched_clean_control,
)
from ditto_screening_protocol.v13_private_package import (
    V13_PRIVATE_PROFILE_SHA256,
    ArtifactCommitment,
    PrivatePackageRegistration,
    V13PrivateManifest,
    V13PrivatePair,
)


class Store:
    def __init__(self) -> None:
        self.manifests: dict[str, bytes] = {}
        self.payloads: dict[str, bytes] = {}

    async def read_manifest(self, sha256: str) -> bytes:
        return self.manifests[sha256]

    async def read_payload(self, sha256: str) -> bytes:
        return self.payloads[sha256]


class Registry:
    def __init__(self) -> None:
        self.registrations: dict[tuple[UUID, str], PrivatePackageRegistration] = {}

    async def get_group_registration(
        self, group_id: UUID, role: str
    ) -> PrivatePackageRegistration:
        return self.registrations[(group_id, role)]


class Approvals:
    def __init__(self, approval: KnownBenignControlApproval) -> None:
        self.approval = approval

    async def get_approval(
        self, _agent_id: UUID, _attempt_id: UUID
    ) -> KnownBenignControlApproval:
        return self.approval


class Generations:
    def __init__(self, group: TrustedGenerationGroup) -> None:
        self.groups = {group.group_id: group}

    async def get_verified_group(self, group_id: UUID) -> TrustedGenerationGroup:
        return self.groups[group_id]


@dataclass
class Fixture:
    group_id: UUID
    target: ArtifactCommitment
    control: ArtifactCommitment
    store: Store
    packages: Registry
    controls: Approvals
    generations: Generations


def _sealed_pairs(store: Store) -> tuple[V13PrivatePair, ...]:
    pairs: list[V13PrivatePair] = []
    for seed in ("1" * 64, "2" * 64):
        for class_name in (
            "field_entity_rename",
            "request_paraphrase",
            "record_reorder_decoy",
        ):
            for index in range(10):
                digests: list[str] = []
                for side in ("control", "variant"):
                    blob = f"synthetic-{seed}-{class_name}-{index}-{side}".encode()
                    digest = hashlib.sha256(blob).hexdigest()
                    store.payloads[digest] = blob
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
    return tuple(pairs)


def _register(
    store: Store,
    packages: Registry,
    group_id: UUID,
    role: Literal["target", "known_benign"],
    commitment: ArtifactCommitment,
    pairs: tuple[V13PrivatePair, ...],
    generated_at: datetime,
    generation_receipt_sha256: str,
) -> PrivatePackageRegistration:
    manifest = V13PrivateManifest(
        agent_id=commitment.agent_id,
        attempt_id=commitment.attempt_id,
        artifact_sha256=commitment.artifact_sha256,
        image_sha256=commitment.image_sha256,
        profile_sha256=V13_PRIVATE_PROFILE_SHA256,
        generated_at=generated_at,
        tool_catalog_applicable=False,
        pairs=pairs,
    )
    raw = json.dumps(
        manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(raw).hexdigest()
    store.manifests[digest] = raw
    registration = PrivatePackageRegistration(
        agent_id=commitment.agent_id,
        attempt_id=commitment.attempt_id,
        artifact_sha256=commitment.artifact_sha256,
        image_sha256=commitment.image_sha256,
        profile_sha256=V13_PRIVATE_PROFILE_SHA256,
        manifest_sha256=digest,
        generation_group_id=group_id,
        generation_role=role,
        generation_receipt_sha256=generation_receipt_sha256,
        registered_at=generated_at + timedelta(minutes=1),
        registrar_id="synthetic-trusted-registry",
    )
    packages.registrations[(group_id, role)] = registration
    return registration


def _fixture() -> Fixture:
    store = Store()
    packages = Registry()
    pairs = _sealed_pairs(store)
    control = ArtifactCommitment(
        agent_id=uuid4(),
        attempt_id=uuid4(),
        artifact_sha256="a" * 64,
        image_sha256="b" * 64,
        committed_at=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
    )
    target = ArtifactCommitment(
        agent_id=uuid4(),
        attempt_id=uuid4(),
        artifact_sha256="c" * 64,
        image_sha256="d" * 64,
        committed_at=datetime(2026, 9, 23, 11, 0, tzinfo=UTC),
    )
    approval = KnownBenignControlApproval(
        approval_id=uuid4(),
        agent_id=control.agent_id,
        attempt_id=control.attempt_id,
        artifact_sha256=control.artifact_sha256,
        image_sha256=control.image_sha256,
        profile_sha256=V13_PRIVATE_PROFILE_SHA256,
        approved_at=datetime(2026, 9, 23, 12, 0, 30, tzinfo=UTC),
        approver_id="synthetic-control-curator",
        approval_receipt_sha256="e" * 64,
    )
    group = TrustedGenerationGroup(
        group_id=uuid4(),
        target_agent_id=target.agent_id,
        target_attempt_id=target.attempt_id,
        target_artifact_sha256=target.artifact_sha256,
        target_image_sha256=target.image_sha256,
        control_agent_id=control.agent_id,
        control_attempt_id=control.attempt_id,
        control_artifact_sha256=control.artifact_sha256,
        control_image_sha256=control.image_sha256,
        approval_id=approval.approval_id,
        profile_sha256=V13_PRIVATE_PROFILE_SHA256,
        started_at=datetime(2026, 9, 23, 12, 0, 45, tzinfo=UTC),
        target_receipt_sha256="0" * 64,
        control_receipt_sha256="0" * 64,
    )
    group = group.model_copy(
        update={
            "target_receipt_sha256": compute_v13_generation_role_digest(
                group, "target"
            ),
            "control_receipt_sha256": compute_v13_generation_role_digest(
                group, "known_benign"
            ),
        }
    )
    _register(
        store,
        packages,
        group.group_id,
        "target",
        target,
        pairs,
        datetime(2026, 9, 23, 13, 0, tzinfo=UTC),
        group.target_receipt_sha256,
    )
    _register(
        store,
        packages,
        group.group_id,
        "known_benign",
        control,
        pairs,
        datetime(2026, 9, 23, 12, 1, tzinfo=UTC),
        group.control_receipt_sha256,
    )
    return Fixture(
        group.group_id,
        target,
        control,
        store,
        packages,
        Approvals(approval),
        Generations(group),
    )


def _prepare(fixture: Fixture) -> V13MatchedCleanControlCommitment:
    return asyncio.run(
        prepare_v13_matched_clean_control(
            group_id=fixture.group_id,
            target=fixture.target,
            control=fixture.control,
            store=fixture.store,
            packages=fixture.packages,
            controls=fixture.controls,
            generations=fixture.generations,
        )
    )


def test_generation_group_role_digest_fixed_vector() -> None:
    group = TrustedGenerationGroup(
        group_id=UUID(int=1),
        target_agent_id=UUID(int=2),
        target_attempt_id=UUID(int=3),
        target_artifact_sha256="a" * 64,
        target_image_sha256="b" * 64,
        control_agent_id=UUID(int=4),
        control_attempt_id=UUID(int=5),
        control_artifact_sha256="c" * 64,
        control_image_sha256="d" * 64,
        approval_id=UUID(int=6),
        profile_sha256="e" * 64,
        started_at=datetime(2026, 9, 23, 12, 0, 45, 123456, tzinfo=UTC),
        target_receipt_sha256="0" * 64,
        control_receipt_sha256="0" * 64,
    )
    assert compute_v13_generation_role_digest(group, "target") == (
        "96fd7f66ddc49df69bce3c57af4a4f75c97fb4a3ed45f98a75d8cb649ac31973"
    )
    assert compute_v13_generation_role_digest(group, "known_benign") == (
        "1575e4205e10e50c2f2b3d3c33e6c1a920aa1897fcdd3c0d3f398c809f304d87"
    )


def test_replay_group_digest_commits_independent_image_and_approval() -> None:
    group = TrustedGenerationGroup(
        group_id=UUID(int=1),
        replay_id=UUID(int=7),
        target_agent_id=UUID(int=2),
        target_attempt_id=UUID(int=3),
        target_artifact_sha256="a" * 64,
        target_image_sha256="b" * 64,
        control_agent_id=UUID(int=4),
        control_attempt_id=UUID(int=5),
        control_artifact_sha256="c" * 64,
        control_image_sha256="d" * 64,
        approval_id=UUID(int=6),
        approval_receipt_sha256="f" * 64,
        profile_sha256="e" * 64,
        started_at=datetime(2026, 9, 23, 12, 0, 45, 123456, tzinfo=UTC),
        target_receipt_sha256="0" * 64,
        control_receipt_sha256="0" * 64,
    )
    target = compute_v13_generation_role_digest(group, "target")
    assert target != compute_v13_generation_role_digest(group, "known_benign")
    assert target != compute_v13_generation_role_digest(
        group.model_copy(update={"replay_id": UUID(int=8)}), "target"
    )
    assert target != compute_v13_generation_role_digest(
        group.model_copy(update={"approval_receipt_sha256": "9" * 64}), "target"
    )


def test_same_sealed_pairs_yield_digest_only_distinct_image_commitment() -> None:
    fixture = _fixture()
    matched = _prepare(fixture)
    assert fixture.target.committed_at < fixture.controls.approval.approved_at
    assert matched.pair_count == 60
    assert matched.target_image_sha256 != matched.control_image_sha256
    assert matched.control_approval_id == fixture.controls.approval.approval_id
    assert (
        matched.control_generation_receipt_sha256
        == fixture.generations.groups[fixture.group_id].control_receipt_sha256
    )
    assert set(matched.model_dump(mode="json")) == set(type(matched).model_fields)
    assert not any(
        name in matched.model_dump(mode="json")
        for name in ("pairs", "seed_commitment", "run_envelope", "answer")
    )


def test_one_approved_benign_image_supports_two_distinct_hidden_groups() -> None:
    fixture = _fixture()
    first = _prepare(fixture)
    second_target = ArtifactCommitment(
        agent_id=uuid4(),
        attempt_id=uuid4(),
        artifact_sha256="f" * 64,
        image_sha256="9" * 64,
        committed_at=datetime(2026, 9, 23, 11, 30, tzinfo=UTC),
    )
    base = fixture.generations.groups[fixture.group_id]
    second_group = base.model_copy(
        update={
            "group_id": uuid4(),
            "target_agent_id": second_target.agent_id,
            "target_attempt_id": second_target.attempt_id,
            "target_artifact_sha256": second_target.artifact_sha256,
            "target_image_sha256": second_target.image_sha256,
            "started_at": datetime(2026, 9, 23, 12, 0, 46, tzinfo=UTC),
        }
    )
    second_group = second_group.model_copy(
        update={
            "target_receipt_sha256": compute_v13_generation_role_digest(
                second_group, "target"
            ),
            "control_receipt_sha256": compute_v13_generation_role_digest(
                second_group, "known_benign"
            ),
        }
    )
    fixture.generations.groups[second_group.group_id] = second_group
    second_pairs = _sealed_pairs(fixture.store)
    _register(
        fixture.store,
        fixture.packages,
        second_group.group_id,
        "target",
        second_target,
        second_pairs,
        datetime(2026, 9, 23, 13, 0, tzinfo=UTC),
        second_group.target_receipt_sha256,
    )
    _register(
        fixture.store,
        fixture.packages,
        second_group.group_id,
        "known_benign",
        fixture.control,
        second_pairs,
        datetime(2026, 9, 23, 12, 1, 30, tzinfo=UTC),
        second_group.control_receipt_sha256,
    )
    second = _prepare(
        Fixture(
            second_group.group_id,
            second_target,
            fixture.control,
            fixture.store,
            fixture.packages,
            fixture.controls,
            fixture.generations,
        )
    )
    assert first.control_image_sha256 == second.control_image_sha256
    assert first.group_id != second.group_id
    assert first.pair_inventory_sha256 != second.pair_inventory_sha256
    fixture.packages.registrations[(second_group.group_id, "known_benign")] = (
        fixture.packages.registrations[(fixture.group_id, "known_benign")]
    )
    with pytest.raises(MatchedCleanControlUnavailable):
        _prepare(
            Fixture(
                second_group.group_id,
                second_target,
                fixture.control,
                fixture.store,
                fixture.packages,
                fixture.controls,
                fixture.generations,
            )
        )


def test_reordered_or_changed_case_inventory_fails_closed() -> None:
    fixture = _fixture()
    control_key = (fixture.group_id, "known_benign")
    old = fixture.packages.registrations[control_key]
    manifest = json.loads(fixture.store.manifests[old.manifest_sha256])
    manifest["pairs"][0], manifest["pairs"][1] = (
        manifest["pairs"][1],
        manifest["pairs"][0],
    )
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    new_digest = hashlib.sha256(raw).hexdigest()
    fixture.store.manifests[new_digest] = raw
    fixture.packages.registrations[control_key] = old.model_copy(
        update={"manifest_sha256": new_digest}
    )
    with pytest.raises(MatchedCleanControlUnavailable, match="inventories"):
        _prepare(fixture)


def test_untrusted_or_late_control_approval_fails_closed() -> None:
    fixture = _fixture()
    fixture.controls.approval = fixture.controls.approval.model_copy(
        update={"image_sha256": "f" * 64}
    )
    with pytest.raises(MatchedCleanControlUnavailable, match="identity"):
        _prepare(fixture)
    fixture = _fixture()
    fixture.controls.approval = fixture.controls.approval.model_copy(
        update={"approved_at": datetime(2026, 9, 23, 11, 0, tzinfo=UTC)}
    )
    with pytest.raises(MatchedCleanControlUnavailable, match="identity"):
        _prepare(fixture)
    fixture = _fixture()
    fixture.controls.approval = fixture.controls.approval.model_copy(
        update={"approved_at": datetime(2026, 9, 23, 12, 0, 45, tzinfo=UTC)}
    )
    with pytest.raises(MatchedCleanControlUnavailable, match="identity"):
        _prepare(fixture)
    fixture = _fixture()
    fixture.controls.approval = fixture.controls.approval.model_copy(
        update={"approved_at": datetime(2026, 9, 23, 12, 1, 30, tzinfo=UTC)}
    )
    with pytest.raises(MatchedCleanControlUnavailable, match="identity"):
        _prepare(fixture)
    fixture = _fixture()
    fixture.controls.approval = fixture.controls.approval.model_copy(
        update={"approved_at": datetime(2026, 9, 23, 13, 0, 1, tzinfo=UTC)}
    )
    with pytest.raises(MatchedCleanControlUnavailable, match="identity"):
        _prepare(fixture)
    fixture = _fixture()
    fixture.controls.approval = fixture.controls.approval.model_copy(
        update={"approved_at": datetime(2026, 9, 23, 14, 0, tzinfo=UTC)}
    )
    with pytest.raises(MatchedCleanControlUnavailable, match="identity"):
        _prepare(fixture)


def test_missing_or_rebound_trusted_generation_receipt_fails_closed() -> None:
    fixture = _fixture()
    key = (fixture.group_id, "known_benign")
    registration = fixture.packages.registrations[key]
    fixture.packages.registrations[key] = registration.model_copy(
        update={"generation_receipt_sha256": None}
    )
    with pytest.raises(MatchedCleanControlUnavailable, match="generation start"):
        _prepare(fixture)
    fixture = _fixture()
    key = (fixture.group_id, "known_benign")
    registration = fixture.packages.registrations[key]
    fixture.packages.registrations[key] = registration.model_copy(
        update={"generation_role": "target"}
    )
    with pytest.raises(MatchedCleanControlUnavailable, match="generation start"):
        _prepare(fixture)
    fixture = _fixture()
    group = fixture.generations.groups[fixture.group_id]
    fixture.generations.groups[fixture.group_id] = group.model_copy(
        update={"target_artifact_sha256": "f" * 64}
    )
    with pytest.raises(MatchedCleanControlUnavailable, match="identity"):
        _prepare(fixture)


def test_tampered_sealed_case_never_produces_matched_commitment() -> None:
    fixture = _fixture()
    digest = next(iter(fixture.store.payloads))
    fixture.store.payloads[digest] = b"private synthetic case mutation"
    with pytest.raises(MatchedCleanControlUnavailable) as error:
        _prepare(fixture)
    assert "private synthetic case mutation" not in str(error.value)
