from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ditto.api_models.coding_evidence import CodingSealedEvidenceKind
from ditto.api_server.coding_hippius_custody import (
    HippiusEvidenceCustodyError,
    HippiusEvidenceSpool,
    RsaOaepHippiusEvidenceKeyWrapper,
    create_hippius_evidence_runtime_from_env,
)
from ditto.api_server.coding_hippius_evidence import (
    HippiusSealedEvidenceSourceAuthority,
    prepare_hippius_sealed_evidence,
)
from ditto.tests.api_server.test_coding_hippius_evidence import _config, _probe


def _public_key(root: Path, name: str) -> Path:
    private = rsa.generate_private_key(public_exponent=65_537, key_size=3072)
    path = root / name
    path.write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    path.chmod(0o600)
    return path


def _spool(root: Path) -> HippiusEvidenceSpool:
    spool_root = root / "spool"
    spool_root.mkdir(mode=0o700)
    spool_root.chmod(0o700)
    return HippiusEvidenceSpool(spool_root)


def _authority() -> HippiusSealedEvidenceSourceAuthority:
    return HippiusSealedEvidenceSourceAuthority(
        ticket_id=UUID("11111111-1111-4111-8111-111111111111"),
        claim_generation=1,
        validator_hotkey="5" + "B" * 47,
        instance_id="coding-custody-worker-001",
        ticket_deadline=datetime(2026, 9, 2, 17, 0, tzinfo=UTC),
        evidence_kind=CodingSealedEvidenceKind.AUTHORING_PUBLICATION_REQUEST,
        weight_eligible=False,
    )


async def test_spool_persists_exact_prepared_bytes_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    wrapper = RsaOaepHippiusEvidenceKeyWrapper(
        _public_key(tmp_path, "evidence-wrap.pem")
    )
    spool = _spool(tmp_path)
    prepared = await prepare_hippius_sealed_evidence(
        authority=_authority(),
        plaintext=b"signed publication request\n",
        key_wrapper=wrapper,
        reservation_id=UUID("22222222-2222-4222-8222-222222222222"),
        random_bytes=lambda size: b"k" * size,
    )

    stored = spool.store(prepared)
    assert stored == prepared
    assert spool.store(prepared) == prepared
    assert spool.load(prepared.identity.identity_sha256) == prepared
    directory = tmp_path / "spool" / prepared.identity.identity_sha256
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "manifest.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((directory / "ciphertext.bin").stat().st_mode) == 0o600
    rendered = repr(spool) + repr(wrapper) + repr(stored)
    assert prepared.remote_key not in rendered
    assert prepared.ciphertext not in rendered.encode()


async def test_spool_rejects_drift_partial_state_and_unsafe_modes(
    tmp_path: Path,
) -> None:
    wrapper = RsaOaepHippiusEvidenceKeyWrapper(
        _public_key(tmp_path, "evidence-wrap.pem")
    )
    spool = _spool(tmp_path)
    prepared = await prepare_hippius_sealed_evidence(
        authority=_authority(),
        plaintext=b"exact evidence",
        key_wrapper=wrapper,
        reservation_id=UUID("33333333-3333-4333-8333-333333333333"),
        random_bytes=lambda size: b"n" * size,
    )
    spool.store(prepared)
    ciphertext = (
        tmp_path / "spool" / prepared.identity.identity_sha256 / "ciphertext.bin"
    )
    ciphertext.write_bytes(ciphertext.read_bytes() + b"x")
    with pytest.raises(HippiusEvidenceCustodyError, match="ciphertext"):
        spool.load(prepared.identity.identity_sha256)

    partial_identity = "f" * 64
    partial = tmp_path / "spool" / partial_identity
    partial.mkdir(mode=0o700)
    with pytest.raises(HippiusEvidenceCustodyError, match="manifest"):
        spool.load(partial_identity)

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    with pytest.raises(HippiusEvidenceCustodyError, match="mode"):
        HippiusEvidenceSpool(unsafe)


async def test_spool_store_retries_after_incomplete_identity_directory(
    tmp_path: Path,
) -> None:
    wrapper = RsaOaepHippiusEvidenceKeyWrapper(
        _public_key(tmp_path, "evidence-wrap.pem")
    )
    spool = _spool(tmp_path)
    prepared = await prepare_hippius_sealed_evidence(
        authority=_authority(),
        plaintext=b"retryable spool persist",
        key_wrapper=wrapper,
        reservation_id=UUID("44444444-4444-4444-8444-444444444444"),
        random_bytes=lambda size: b"r" * size,
    )
    incomplete = tmp_path / "spool" / prepared.identity.identity_sha256
    incomplete.mkdir(mode=0o700)
    (incomplete / "ciphertext.bin").write_bytes(b"partial")
    recovered = spool.store(prepared)
    assert recovered == prepared
    assert (incomplete / "ciphertext.bin").read_bytes() == prepared.ciphertext
    assert (incomplete / "manifest.json").is_file()
    assert spool.store(prepared) == prepared


async def test_rotation_changes_new_wrap_identity_without_breaking_old_spool(
    tmp_path: Path,
) -> None:
    first_wrapper = RsaOaepHippiusEvidenceKeyWrapper(_public_key(tmp_path, "first.pem"))
    second_wrapper = RsaOaepHippiusEvidenceKeyWrapper(
        _public_key(tmp_path, "second.pem")
    )
    assert first_wrapper.wrapping_key_sha256 != second_wrapper.wrapping_key_sha256
    spool = _spool(tmp_path)
    prepared = await prepare_hippius_sealed_evidence(
        authority=_authority(),
        plaintext=b"old rotation evidence",
        key_wrapper=first_wrapper,
        reservation_id=UUID("44444444-4444-4444-8444-444444444444"),
    )
    spool.store(prepared)

    assert spool.load(prepared.identity.identity_sha256) == prepared
    assert prepared.identity.wrapping_key_sha256 == first_wrapper.wrapping_key_sha256


def test_default_off_runtime_requires_complete_redacted_custody(tmp_path: Path) -> None:
    assert (
        create_hippius_evidence_runtime_from_env(
            session_maker=MagicMock(),
            environ={},
        )
        is None
    )
    config = _config()
    probe_path = _probe(tmp_path, config)
    spool_root = tmp_path / "runtime-spool"
    spool_root.mkdir(mode=0o700)
    spool_root.chmod(0o700)
    public_key = _public_key(tmp_path, "runtime-wrap.pem")
    environ = {
        "DITTO_CODING_HIPPIUS_EVIDENCE_ENABLED": "true",
        "DITTO_CODING_HIPPIUS_ENDPOINT_URL": config.endpoint_url,
        "DITTO_CODING_HIPPIUS_REGION": config.region,
        "DITTO_CODING_HIPPIUS_SEALED_EVIDENCE_BUCKET": config.bucket,
        "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY": (
            config.mediator.access_key
        ),
        "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY": (
            config.mediator.secret_key
        ),
        "DITTO_CODING_HIPPIUS_PROBE_RECEIPT_PATH": str(probe_path),
        "DITTO_CODING_HIPPIUS_EVIDENCE_SPOOL_ROOT": str(spool_root),
        "DITTO_CODING_HIPPIUS_EVIDENCE_WRAPPING_PUBLIC_KEY_PATH": str(public_key),
    }
    runtime = create_hippius_evidence_runtime_from_env(
        session_maker=MagicMock(),
        environ=environ,
    )
    assert runtime is not None
    assert runtime.readiness.configured is True
    assert runtime.readiness.runtime_wired is True
    assert runtime.readiness.worker_active is False
    assert runtime.readiness.weight_eligible is False
    rendered = repr(runtime) + repr(runtime.readiness)
    for secret in (
        config.endpoint_url,
        config.bucket,
        config.mediator.access_key,
        config.mediator.secret_key,
        str(spool_root),
        str(public_key),
    ):
        assert secret not in rendered

    incomplete = dict(environ)
    incomplete.pop("DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY")
    with pytest.raises(HippiusEvidenceCustodyError, match="incomplete"):
        create_hippius_evidence_runtime_from_env(
            session_maker=MagicMock(),
            environ=incomplete,
        )


def test_spool_rejects_symlink_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    target.chmod(0o700)
    link = tmp_path / "link"
    os.symlink(target, link)
    with pytest.raises(HippiusEvidenceCustodyError, match="root"):
        HippiusEvidenceSpool(link)
