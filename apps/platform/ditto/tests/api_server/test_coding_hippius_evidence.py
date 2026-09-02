from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from ditto.api_models.coding_evidence import (
    CodingSealedEvidenceIdentity,
    CodingSealedEvidenceKind,
)
from ditto.api_server.coding_hippius_evidence import (
    HippiusSealedEvidenceConfig,
    HippiusSealedEvidenceConflict,
    HippiusSealedEvidenceMediator,
    HippiusSealedEvidenceNotFound,
    HippiusSealedEvidencePreparedObject,
    HippiusSealedEvidenceSourceAuthority,
    HippiusSealedEvidenceStatus,
    HippiusSealedEvidenceUnavailable,
    parse_hippius_sealed_evidence_config,
    prepare_hippius_sealed_evidence,
)
from ditto.api_server.coding_hippius_probe import (
    HIPPIUS_REVIEWED_REVISION,
    HippiusProbeCheck,
    HippiusProbeCheckStatus,
    HippiusProbeCredential,
    HippiusProbeReceipt,
    write_hippius_probe_receipt,
)


def _config(**overrides: object) -> HippiusSealedEvidenceConfig:
    values: dict[str, object] = {
        "endpoint_url": "https://s3.hippius.com",
        "bucket": "coding-sealed-evidence",
        "mediator": HippiusProbeCredential(
            access_key="hip_evidence_mediator",
            secret_key="evidence-mediator-secret",
        ),
        "region": "decentralized",
        "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return HippiusSealedEvidenceConfig(**values)  # type: ignore[arg-type]


def _probe(root: Path, config: HippiusSealedEvidenceConfig) -> Path:
    path = (root / "probe.json").resolve()
    write_hippius_probe_receipt(
        receipt=HippiusProbeReceipt(
            schema="dittobench-coding-hippius-capability-probe-v1",
            source_sha="a" * 40,
            reviewed_hippius_revision=HIPPIUS_REVIEWED_REVISION,
            checked_at="2026-09-02T15:00:00Z",
            provider="hippius",
            private_input_authority_sha256="b" * 64,
            sealed_evidence_authority_sha256=config.authority_sha256,
            synthetic_only=True,
            retained_synthetic_objects=2,
            ready=True,
            weight_eligible=False,
            checks=(
                HippiusProbeCheck(
                    name="synthetic_provider_probe",
                    status=HippiusProbeCheckStatus.PASS,
                    detail="verified",
                ),
            ),
        ),
        output=path,
    )
    return path


def _authority(**overrides: object) -> HippiusSealedEvidenceSourceAuthority:
    values: dict[str, object] = {
        "ticket_id": UUID("11111111-1111-4111-8111-111111111111"),
        "claim_generation": 2,
        "validator_hotkey": "5" + "B" * 47,
        "instance_id": "coding-worker-evidence-001",
        "ticket_deadline": datetime(2026, 9, 2, 17, 0, tzinfo=UTC),
        "evidence_kind": CodingSealedEvidenceKind.AUTHORING_PUBLICATION_REQUEST,
        "weight_eligible": False,
    }
    values.update(overrides)
    return HippiusSealedEvidenceSourceAuthority(**values)  # type: ignore[arg-type]


@dataclass
class _Wrapper:
    wrapping_key_sha256: str = "c" * 64

    def __post_init__(self) -> None:
        self.calls: list[tuple[bytes, str]] = []

    async def wrap_data_key(self, *, data_key: bytes, aad_sha256: str) -> bytes:
        self.calls.append((data_key, aad_sha256))
        return b"wrapped:" + data_key


class _Transport:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_count = 0
        self.ambiguous_put = False

    async def get_object(self, *, key: str, max_bytes: int) -> bytes:
        try:
            body = self.objects[key]
        except KeyError as error:
            raise HippiusSealedEvidenceNotFound("missing") from error
        assert len(body) <= max_bytes
        return body

    async def put_object(
        self,
        *,
        key: str,
        body: bytes,
        metadata: Mapping[str, str],
    ) -> None:
        assert metadata["ciphertext-sha256"]
        self.put_count += 1
        self.objects[key] = body
        if self.ambiguous_put:
            raise HippiusSealedEvidenceUnavailable("ambiguous")


class _Ledger:
    def __init__(self) -> None:
        self.reservations: dict[
            tuple[UUID, int, CodingSealedEvidenceKind], CodingSealedEvidenceIdentity
        ] = {}
        self.finalizations: dict[str, HippiusSealedEvidenceStatus] = {}

    async def reserve(self, identity: CodingSealedEvidenceIdentity) -> None:
        key = (
            identity.ticket_id,
            identity.claim_generation,
            identity.evidence_kind,
        )
        existing = self.reservations.get(key)
        if existing is not None and existing != identity:
            raise HippiusSealedEvidenceConflict("reservation drift")
        self.reservations[key] = identity

    async def finalize(
        self,
        identity: CodingSealedEvidenceIdentity,
        status: HippiusSealedEvidenceStatus,
    ) -> HippiusSealedEvidenceStatus:
        existing = self.finalizations.get(identity.identity_sha256)
        if existing is not None:
            return existing
        self.finalizations[identity.identity_sha256] = status
        return status


def _random_bytes(size: int) -> bytes:
    if size == 32:
        return b"k" * 32
    if size == 12:
        return b"n" * 12
    raise AssertionError("unexpected entropy request")


async def _prepared() -> HippiusSealedEvidencePreparedObject:
    return await prepare_hippius_sealed_evidence(
        authority=_authority(),
        plaintext=b'{"signed":"exact publication bytes"}\n',
        key_wrapper=_Wrapper(),
        reservation_id=UUID("22222222-2222-4222-8222-222222222222"),
        random_bytes=_random_bytes,
    )


async def test_mediator_reserves_uploads_verifies_finalizes_and_replays(
    tmp_path: Path,
) -> None:
    config = _config()
    prepared = await _prepared()
    transport = _Transport()
    ledger = _Ledger()
    mediator = HippiusSealedEvidenceMediator(
        config=config,
        probe_receipt_path=_probe(tmp_path, config),
        transport=transport,
        ledger=ledger,
    )

    first = await mediator.publish(
        prepared=prepared,
        now=datetime(2026, 9, 2, 16, 0, tzinfo=UTC),
    )
    assert first.status is HippiusSealedEvidenceStatus.UPLOADED
    assert first.ready is True and first.weight_eligible is False
    assert transport.put_count == 1
    assert next(iter(transport.objects.values())) == prepared.ciphertext
    assert len(ledger.reservations) == 1
    assert len(ledger.finalizations) == 1

    second = await mediator.publish(
        prepared=prepared,
        now=datetime(2026, 9, 2, 16, 1, tzinfo=UTC),
    )
    assert second.status is HippiusSealedEvidenceStatus.UPLOADED
    assert transport.put_count == 1
    assert "coding-sealed-evidence" not in repr(prepared)
    assert prepared.remote_key not in repr(prepared)
    assert prepared.ciphertext not in repr(prepared).encode()


async def test_ambiguous_upload_retains_reservation_and_reuses_exact_bytes(
    tmp_path: Path,
) -> None:
    config = _config()
    prepared = await _prepared()
    transport = _Transport()
    transport.ambiguous_put = True
    ledger = _Ledger()
    mediator = HippiusSealedEvidenceMediator(
        config=config,
        probe_receipt_path=_probe(tmp_path, config),
        transport=transport,
        ledger=ledger,
    )
    with pytest.raises(HippiusSealedEvidenceUnavailable):
        await mediator.publish(
            prepared=prepared,
            now=datetime(2026, 9, 2, 16, 0, tzinfo=UTC),
        )
    assert len(ledger.reservations) == 1
    assert not ledger.finalizations
    assert transport.objects[prepared.remote_key] == prepared.ciphertext

    transport.ambiguous_put = False
    recovered = await mediator.publish(
        prepared=prepared,
        now=datetime(2026, 9, 2, 16, 1, tzinfo=UTC),
    )
    assert recovered.status is HippiusSealedEvidenceStatus.REUSED
    assert transport.put_count == 1


async def test_mediator_refuses_conflict_stale_probe_and_prepared_drift(
    tmp_path: Path,
) -> None:
    config = _config()
    prepared = await _prepared()
    transport = _Transport()
    transport.objects[prepared.remote_key] = b"different"
    ledger = _Ledger()
    mediator = HippiusSealedEvidenceMediator(
        config=config,
        probe_receipt_path=_probe(tmp_path, config),
        transport=transport,
        ledger=ledger,
    )
    with pytest.raises(HippiusSealedEvidenceConflict, match="different"):
        await mediator.publish(
            prepared=prepared,
            now=datetime(2026, 9, 2, 16, 0, tzinfo=UTC),
        )
    assert not ledger.finalizations
    with pytest.raises(HippiusSealedEvidenceUnavailable, match="freshness"):
        await mediator.publish(
            prepared=prepared,
            now=datetime(2026, 9, 4, 16, 0, tzinfo=UTC),
        )
    with pytest.raises(HippiusSealedEvidenceConflict, match="bytes"):
        await mediator.publish(
            prepared=replace(prepared, ciphertext=prepared.ciphertext + b"x"),
            now=datetime(2026, 9, 2, 16, 0, tzinfo=UTC),
        )


async def test_preparation_uses_fresh_encryption_identity_and_kind_bounds() -> None:
    wrapper = _Wrapper()
    prepared = await prepare_hippius_sealed_evidence(
        authority=_authority(),
        plaintext=b"exact evidence",
        key_wrapper=wrapper,
        reservation_id=UUID("33333333-3333-4333-8333-333333333333"),
        random_bytes=_random_bytes,
    )
    assert prepared.identity.plaintext_size_bytes == len(b"exact evidence")
    assert prepared.identity.ciphertext_size_bytes == len(b"exact evidence") + 16
    assert prepared.identity.weight_eligible is False
    assert len(wrapper.calls) == 1
    with pytest.raises(HippiusSealedEvidenceConflict, match="kind bound"):
        await prepare_hippius_sealed_evidence(
            authority=_authority(
                evidence_kind=(
                    CodingSealedEvidenceKind.TERMINAL_PUBLICATION_ACKNOWLEDGEMENT
                )
            ),
            plaintext=b"x" * ((1 << 20) + 1),
            key_wrapper=wrapper,
        )


def test_evidence_config_is_hippius_only_redacted_and_separate() -> None:
    config = _config()
    loaded = parse_hippius_sealed_evidence_config(
        {
            "DITTO_CODING_HIPPIUS_ENDPOINT_URL": config.endpoint_url,
            "DITTO_CODING_HIPPIUS_REGION": config.region,
            "DITTO_CODING_HIPPIUS_SEALED_EVIDENCE_BUCKET": config.bucket,
            "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY": (
                config.mediator.access_key
            ),
            "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY": (
                config.mediator.secret_key
            ),
        }
    )
    assert loaded.authority_sha256 == config.authority_sha256
    for sensitive in (
        config.endpoint_url,
        config.bucket,
        config.mediator.access_key,
        config.mediator.secret_key,
    ):
        assert sensitive not in repr(config)
    with pytest.raises(HippiusSealedEvidenceConflict, match="configuration"):
        _config(endpoint_url="https://evil.example")


def test_mediator_rejects_probe_authority_drift(tmp_path: Path) -> None:
    config = _config()
    with pytest.raises(HippiusSealedEvidenceConflict, match="bind"):
        HippiusSealedEvidenceMediator(
            config=_config(bucket="different-evidence"),
            probe_receipt_path=_probe(tmp_path, config),
            transport=_Transport(),
            ledger=_Ledger(),
        )
