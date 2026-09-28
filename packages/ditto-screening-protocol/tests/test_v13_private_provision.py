"""Protected V13 provisioning tests use synthetic values, never real challenges."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from ditto_screening_protocol.v13_private_clean_control import (
    TrustedGenerationGroup,
    compute_v13_generation_role_digest,
)
from ditto_screening_protocol.v13_private_execute import (
    IsolatedCaseObservation,
    PrivateExecutionUnavailable,
    execute_v13_private_pairs,
)
from ditto_screening_protocol.v13_private_package import (
    V13_PRIVATE_PROFILE_SHA256,
    ArtifactCommitment,
    PrivatePackageRegistration,
    _prepare_registered_package,
    prepare_sealed_v13_package,
)
from ditto_screening_protocol.v13_private_provision import (
    PrivateBlueprintPair,
    PrivateProvisioningUnavailable,
    provision_v13_matched_private_packages,
    provision_v13_private_package,
)


def _payload(pair_id: UUID, name: str, index: int, side: str) -> bytes:
    answer = f"synthetic-answer:{name}:{index}"
    return json.dumps(
        {
            "revision": "v13-private-case-v1",
            "pair_id": str(pair_id),
            "side": side,
            "semantic_contract_sha256": hashlib.sha256(
                f"synthetic-semantic:{name}:{index}".encode()
            ).hexdigest(),
            "seed_envelope": None,
            "run_envelope": {"synthetic_expected": answer},
            "expected_answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


class SyntheticBank:
    def __init__(self, *, classes: int = 3, pairs_per_class: int = 20) -> None:
        names = (
            "field_entity_rename",
            "request_paraphrase",
            "record_reorder_decoy",
            "catalog_reorder_alias",
        )[:classes]
        pairs: list[PrivateBlueprintPair] = []
        for name in names:
            for index in range(pairs_per_class):
                pair_id = uuid5(NAMESPACE_URL, f"synthetic:{name}:{index}")
                pairs.append(
                    PrivateBlueprintPair(
                        pair_id=pair_id,
                        transformation_class=name,  # type: ignore[arg-type]
                        control=_payload(pair_id, name, index, "control"),
                        variant=_payload(pair_id, name, index, "variant"),
                    )
                )
        self.pairs = tuple(pairs)

    async def load(self) -> tuple[PrivateBlueprintPair, ...]:
        return self.pairs


class MemoryPublisher:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.seeds: dict[str, bytes] = {}
        self.registration: PrivatePackageRegistration | None = None

    async def put_blob(self, digest: str, content: bytes) -> None:
        assert hashlib.sha256(content).hexdigest() == digest
        self.blobs[digest] = content

    async def put_hidden_seed(self, commitment: str, seed: bytes) -> None:
        assert hashlib.sha256(seed).hexdigest() == commitment
        self.seeds[commitment] = seed

    async def register(self, registration: PrivatePackageRegistration) -> None:
        assert self.registration is None
        self.registration = registration

    async def get_registration(
        self, _agent_id: UUID, _attempt_id: UUID
    ) -> PrivatePackageRegistration:
        assert self.registration is not None
        return self.registration

    async def read_manifest(self, sha256: str) -> bytes:
        return self.blobs[sha256]

    async def read_payload(self, sha256: str) -> bytes:
        return self.blobs[sha256]


class MatchedMemoryPublisher(MemoryPublisher):
    def __init__(self) -> None:
        super().__init__()
        self.registrations: list[tuple[PrivatePackageRegistration, str]] = []

    async def register_group(
        self, registration: PrivatePackageRegistration, pair_inventory_sha256: str
    ) -> None:
        self.registrations.append((registration, pair_inventory_sha256))


def _matched_group(
    target: ArtifactCommitment, control: ArtifactCommitment
) -> TrustedGenerationGroup:
    group = TrustedGenerationGroup(
        group_id=uuid5(NAMESPACE_URL, "synthetic-group"),
        replay_id=uuid5(NAMESPACE_URL, "synthetic-replay"),
        target_agent_id=target.agent_id,
        target_attempt_id=target.attempt_id,
        target_artifact_sha256=target.artifact_sha256,
        target_image_sha256=target.image_sha256,
        control_agent_id=control.agent_id,
        control_attempt_id=control.attempt_id,
        control_artifact_sha256=control.artifact_sha256,
        control_image_sha256=control.image_sha256,
        approval_id=uuid5(NAMESPACE_URL, "synthetic-approval"),
        approval_receipt_sha256="c" * 64,
        profile_sha256=V13_PRIVATE_PROFILE_SHA256,
        started_at=datetime.now(UTC) - timedelta(milliseconds=100),
        target_receipt_sha256="0" * 64,
        control_receipt_sha256="0" * 64,
    )
    return group.model_copy(
        update={
            "target_receipt_sha256": compute_v13_generation_role_digest(
                group, "target"
            ),
            "control_receipt_sha256": compute_v13_generation_role_digest(
                group, "known_benign"
            ),
        }
    )


@pytest.mark.parametrize("tool_catalog_applicable", [False, True])
def test_matched_provisioning_registers_one_inventory_for_both_roles(
    tool_catalog_applicable: bool,
) -> None:
    target = _commitment()
    control = target.model_copy(
        update={
            "agent_id": uuid5(NAMESPACE_URL, "synthetic-control-agent"),
            "attempt_id": uuid5(NAMESPACE_URL, "synthetic-control-attempt"),
            "artifact_sha256": "d" * 64,
            "image_sha256": "e" * 64,
        }
    )
    publisher = MatchedMemoryPublisher()
    result = asyncio.run(
        provision_v13_matched_private_packages(
            group=_matched_group(target, control),
            target=target,
            known_benign=control,
            bank=SyntheticBank(classes=4 if tool_catalog_applicable else 3),
            publisher=publisher,
            registrar_id="trusted-test-registrar",
            tool_catalog_applicable=tool_catalog_applicable,
        )
    )
    assert len(publisher.registrations) == 2
    assert {item[1] for item in publisher.registrations} == {
        result.pair_inventory_sha256
    }
    target_package = asyncio.run(
        _prepare_registered_package(
            store=publisher, commitment=target, registration=result.target
        )
    )
    control_package = asyncio.run(
        _prepare_registered_package(
            store=publisher,
            commitment=control,
            registration=result.known_benign,
        )
    )
    assert target_package.manifest.pairs == control_package.manifest.pairs
    assert result.pair_count == (80 if tool_catalog_applicable else 60)
    assert result.target.manifest_sha256 != result.known_benign.manifest_sha256


def test_matched_provisioning_rejects_tampered_group_before_bank_access() -> None:
    target = _commitment()
    control = target.model_copy(
        update={
            "agent_id": uuid5(NAMESPACE_URL, "synthetic-control-agent"),
            "attempt_id": uuid5(NAMESPACE_URL, "synthetic-control-attempt"),
            "artifact_sha256": "d" * 64,
            "image_sha256": "e" * 64,
        }
    )
    group = _matched_group(target, control).model_copy(
        update={"target_receipt_sha256": "f" * 64}
    )

    class InaccessibleBank:
        async def load(self) -> tuple[PrivateBlueprintPair, ...]:
            raise AssertionError("bank must not be read")

    publisher = MatchedMemoryPublisher()
    with pytest.raises(PrivateProvisioningUnavailable, match="trusted generation"):
        asyncio.run(
            provision_v13_matched_private_packages(
                group=group,
                target=target,
                known_benign=control,
                bank=InaccessibleBank(),
                publisher=publisher,
                registrar_id="trusted-test-registrar",
                tool_catalog_applicable=False,
            )
        )
    assert publisher.blobs == {} and publisher.registrations == []


class SyntheticFreshExecutor:
    def __init__(self, *, model_authority: bool = True) -> None:
        self.calls = 0
        self.model_authority = model_authority

    async def run_case(
        self,
        *,
        image_sha256: str,
        seed_envelope: object,
        run_envelope: object,
        timeout_seconds: float,
    ) -> IsolatedCaseObservation:
        assert image_sha256 == "b" * 64
        assert seed_envelope is None
        assert isinstance(run_envelope, dict)
        assert timeout_seconds == 30.0
        self.calls += 1
        return IsolatedCaseObservation(
            response={"answer": run_envelope["synthetic_expected"]},
            model_authority_verified=self.model_authority,
        )


def _commitment() -> ArtifactCommitment:
    return ArtifactCommitment(
        agent_id=uuid5(NAMESPACE_URL, "synthetic-agent"),
        attempt_id=uuid5(NAMESPACE_URL, "synthetic-attempt"),
        artifact_sha256="a" * 64,
        image_sha256="b" * 64,
        committed_at=datetime.now(UTC) - timedelta(seconds=1),
    )


@pytest.mark.parametrize("tool_catalog_applicable", [False, True])
def test_private_rotations_are_disjoint_and_preflight_bound(
    tool_catalog_applicable: bool,
) -> None:
    commitment = _commitment()
    publisher = MemoryPublisher()
    registration = asyncio.run(
        provision_v13_private_package(
            commitment=commitment,
            bank=SyntheticBank(classes=4 if tool_catalog_applicable else 3),
            publisher=publisher,
            registrar_id="trusted-test-registrar",
            tool_catalog_applicable=tool_catalog_applicable,
        )
    )
    prepared = asyncio.run(
        prepare_sealed_v13_package(
            store=publisher,
            commitment=commitment,
            registry=publisher,
        )
    )
    assert registration == publisher.registration
    assert prepared.registration == registration
    assert len(prepared.manifest.pairs) == (80 if tool_catalog_applicable else 60)
    assert len({pair.pair_id for pair in prepared.manifest.pairs}) == len(
        prepared.manifest.pairs
    )
    assert {pair.seed_commitment for pair in prepared.manifest.pairs} == set(
        publisher.seeds
    )
    assert commitment.committed_at < prepared.manifest.generated_at
    assert prepared.manifest.generated_at <= registration.registered_at
    executor = SyntheticFreshExecutor()
    result = asyncio.run(
        execute_v13_private_pairs(
            prepared=prepared,
            store=publisher,
            executor=executor,
            runner_hotkey="trusted-test-runner",
        )
    )
    assert result.summary.status == "completed"
    assert result.summary.completed_pairs == len(prepared.manifest.pairs)
    assert executor.calls == 2 * len(prepared.manifest.pairs)
    assert all(
        row.control_correct == row.variant_correct == row.pairs
        for row in result.aggregates
    )


def test_private_bank_shortfall_never_registers() -> None:
    publisher = MemoryPublisher()
    with pytest.raises(PrivateProvisioningUnavailable, match="bank coverage"):
        asyncio.run(
            provision_v13_private_package(
                commitment=_commitment(),
                bank=SyntheticBank(pairs_per_class=19),
                publisher=publisher,
                registrar_id="trusted-test-registrar",
                tool_catalog_applicable=False,
            )
        )
    assert publisher.registration is None
    assert publisher.blobs == {}


def test_private_runner_requires_model_authority_and_rechecks_payload() -> None:
    commitment = _commitment()
    publisher = MemoryPublisher()
    asyncio.run(
        provision_v13_private_package(
            commitment=commitment,
            bank=SyntheticBank(),
            publisher=publisher,
            registrar_id="trusted-test-registrar",
            tool_catalog_applicable=False,
        )
    )
    prepared = asyncio.run(
        prepare_sealed_v13_package(
            store=publisher, commitment=commitment, registry=publisher
        )
    )
    with pytest.raises(PrivateExecutionUnavailable, match="model authority"):
        asyncio.run(
            execute_v13_private_pairs(
                prepared=prepared,
                store=publisher,
                executor=SyntheticFreshExecutor(model_authority=False),
                runner_hotkey="trusted-test-runner",
            )
        )
    first = prepared.manifest.pairs[0]
    publisher.blobs[first.control_sha256] = b"tampered after preflight"
    with pytest.raises(PrivateExecutionUnavailable, match="unavailable"):
        asyncio.run(
            execute_v13_private_pairs(
                prepared=prepared,
                store=publisher,
                executor=SyntheticFreshExecutor(),
                runner_hotkey="trusted-test-runner",
            )
        )


def test_private_runner_rechecks_manifest_and_prepared_inventory() -> None:
    commitment = _commitment()
    publisher = MemoryPublisher()
    asyncio.run(
        provision_v13_private_package(
            commitment=commitment,
            bank=SyntheticBank(),
            publisher=publisher,
            registrar_id="trusted-test-registrar",
            tool_catalog_applicable=False,
        )
    )
    prepared = asyncio.run(
        prepare_sealed_v13_package(
            store=publisher, commitment=commitment, registry=publisher
        )
    )
    forged_manifest = prepared.manifest.model_copy(
        update={"pairs": tuple(reversed(prepared.manifest.pairs))}
    )
    executor = SyntheticFreshExecutor()
    with pytest.raises(PrivateExecutionUnavailable, match="prepared package"):
        asyncio.run(
            execute_v13_private_pairs(
                prepared=prepared.model_copy(update={"manifest": forged_manifest}),
                store=publisher,
                executor=executor,
                runner_hotkey="trusted-test-runner",
            )
        )
    assert executor.calls == 0

    stale_registration = prepared.registration.model_copy(
        update={"image_sha256": "f" * 64}
    )
    with pytest.raises(PrivateExecutionUnavailable, match="unavailable"):
        asyncio.run(
            execute_v13_private_pairs(
                prepared=prepared.model_copy(
                    update={"registration": stale_registration}
                ),
                store=publisher,
                executor=executor,
                runner_hotkey="trusted-test-runner",
            )
        )
    assert executor.calls == 0

    publisher.blobs[prepared.registration.manifest_sha256] = b"replaced manifest"
    with pytest.raises(PrivateExecutionUnavailable, match="unavailable"):
        asyncio.run(
            execute_v13_private_pairs(
                prepared=prepared,
                store=publisher,
                executor=executor,
                runner_hotkey="trusted-test-runner",
            )
        )
    assert executor.calls == 0
