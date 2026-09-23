"""Synthetic issuer tests; no hidden production cases, keys, or endpoints."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import pytest

from ditto_screening_protocol.v13_private_clean_control import (
    compute_v13_generation_role_digest,
)
from ditto_screening_protocol.v13_private_package import V13_PRIVATE_PROFILE_SHA256
from ditto_screening_protocol.v13_private_seed import (
    AuthenticatedKnownBenignApproval,
    SealedSeedRecord,
    SeedGenerationGroup,
    V13SeedIssuanceUnavailable,
    VerifiedRoleCommitment,
    issue_v13_hidden_group_seeds,
)


class Registry:
    def __init__(
        self,
        group: SeedGenerationGroup,
        target: VerifiedRoleCommitment,
        control: VerifiedRoleCommitment,
        approval: AuthenticatedKnownBenignApproval,
    ) -> None:
        self.group = group
        self.roles = {"target": target, "known_benign": control}
        self.approval = approval

    async def get_verified_group(self, group_id: UUID) -> SeedGenerationGroup:
        assert group_id == self.group.group_id
        return self.group

    async def get_verified_role_commitment(
        self, _group_id: UUID, role: Literal["target", "known_benign"]
    ) -> VerifiedRoleCommitment:
        return self.roles[role]

    async def get_authenticated_approval(
        self, _approval_id: UUID
    ) -> AuthenticatedKnownBenignApproval:
        return self.approval


class Store:
    def __init__(self, committed_at: datetime) -> None:
        self.committed_at = committed_at
        self.records: dict[UUID, SealedSeedRecord] = {}
        self.create_calls = 0

    async def create_once(self, group_id: UUID, new_bundle):
        if group_id not in self.records:
            self.create_calls += 1
            self.records[group_id] = SealedSeedRecord(
                sealed_bytes=new_bundle(), committed_at=self.committed_at
            )
        return self.records[group_id]


def fixture() -> tuple[Registry, Store]:
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    target = VerifiedRoleCommitment(
        agent_id=uuid4(),
        attempt_id=uuid4(),
        artifact_sha256="a" * 64,
        image_sha256="b" * 64,
        committed_at=now,
        policy_version=13,
        image_verification_receipt_sha256="1" * 64,
    )
    control = VerifiedRoleCommitment(
        agent_id=uuid4(),
        attempt_id=uuid4(),
        artifact_sha256="c" * 64,
        image_sha256="d" * 64,
        committed_at=now,
        policy_version=13,
        image_verification_receipt_sha256="2" * 64,
    )
    approval = AuthenticatedKnownBenignApproval(
        approval_id=uuid4(),
        agent_id=control.agent_id,
        attempt_id=control.attempt_id,
        artifact_sha256=control.artifact_sha256,
        image_sha256=control.image_sha256,
        profile_sha256=V13_PRIVATE_PROFILE_SHA256,
        approved_at=now + timedelta(minutes=1),
        approver_id="operator",
        approval_receipt_sha256="e" * 64,
        provenance_status="two_person_authenticated",
        provenance_receipt_sha256="f" * 64,
        completed_at=now + timedelta(minutes=2),
    )
    group = SeedGenerationGroup(
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
        approval_receipt_sha256=approval.approval_receipt_sha256,
        profile_sha256=V13_PRIVATE_PROFILE_SHA256,
        started_at=now + timedelta(minutes=3),
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
    return Registry(group, target, control, approval), Store(now + timedelta(minutes=4))


@pytest.mark.asyncio
async def test_group_issues_one_shared_two_seed_bundle_once() -> None:
    registry, store = fixture()
    first = await issue_v13_hidden_group_seeds(
        group_id=registry.group.group_id, registry=registry, store=store
    )
    second = await issue_v13_hidden_group_seeds(
        group_id=registry.group.group_id, registry=registry, store=store
    )
    assert first == second
    assert store.create_calls == 1
    assert len(set(first.seed_commitments)) == 2
    assert first.target_receipt_sha256 == registry.group.target_receipt_sha256
    assert first.control_receipt_sha256 == registry.group.control_receipt_sha256
    assert "seeds_hex" not in first.model_dump_json()
    assert "seed" not in repr(store.records[registry.group.group_id])
    sealed = json.loads(store.records[registry.group.group_id].sealed_bytes)
    assert len(sealed["seeds_hex"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "approval_receipt",
        "approval_identity",
        "late_attestation",
        "target_image",
        "group_receipt",
        "group_clock",
        "profile",
    ],
)
async def test_prerequisite_fault_never_calls_seed_store(fault: str) -> None:
    registry, store = fixture()
    if fault == "approval_receipt":
        registry.approval = registry.approval.model_copy(
            update={"approval_receipt_sha256": "0" * 64}
        )
    elif fault == "approval_identity":
        registry.approval = registry.approval.model_copy(update={"agent_id": uuid4()})
    elif fault == "late_attestation":
        registry.approval = registry.approval.model_copy(
            update={"completed_at": registry.group.started_at + timedelta(seconds=1)}
        )
    elif fault == "target_image":
        registry.roles["target"] = registry.roles["target"].model_copy(
            update={"image_sha256": "0" * 64}
        )
    elif fault == "group_receipt":
        registry.group = registry.group.model_copy(
            update={"target_receipt_sha256": "0" * 64}
        )
    elif fault == "group_clock":
        registry.group = registry.group.model_copy(
            update={"started_at": registry.roles["target"].committed_at}
        )
    elif fault == "profile":
        registry.group = registry.group.model_copy(update={"profile_sha256": "0" * 64})
    with pytest.raises(V13SeedIssuanceUnavailable):
        await issue_v13_hidden_group_seeds(
            group_id=registry.group.group_id, registry=registry, store=store
        )
    assert store.create_calls == 0


@pytest.mark.asyncio
async def test_existing_seed_bundle_cannot_be_spliced_into_another_group() -> None:
    first_registry, store = fixture()
    await issue_v13_hidden_group_seeds(
        group_id=first_registry.group.group_id, registry=first_registry, store=store
    )
    second_registry, _ = fixture()
    store.records[second_registry.group.group_id] = store.records[
        first_registry.group.group_id
    ]
    with pytest.raises(V13SeedIssuanceUnavailable):
        await issue_v13_hidden_group_seeds(
            group_id=second_registry.group.group_id,
            registry=second_registry,
            store=store,
        )
    assert store.create_calls == 1


@pytest.mark.asyncio
async def test_sealed_store_must_commit_after_group_start() -> None:
    registry, store = fixture()
    store.committed_at = registry.group.started_at
    with pytest.raises(V13SeedIssuanceUnavailable):
        await issue_v13_hidden_group_seeds(
            group_id=registry.group.group_id, registry=registry, store=store
        )


@pytest.mark.asyncio
async def test_registry_failure_does_not_generate_seed(monkeypatch) -> None:
    registry, store = fixture()

    async def fail(_group_id: UUID) -> SeedGenerationGroup:
        raise RuntimeError("private approval response with secret")

    monkeypatch.setattr(registry, "get_verified_group", fail)
    with pytest.raises(V13SeedIssuanceUnavailable) as raised:
        await issue_v13_hidden_group_seeds(
            group_id=registry.group.group_id, registry=registry, store=store
        )
    assert "secret" not in str(raised.value)
    assert store.create_calls == 0
