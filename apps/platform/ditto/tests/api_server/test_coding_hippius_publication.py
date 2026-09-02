from __future__ import annotations

import hashlib
import importlib.util
import os
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ditto.api_server.coding_hippius_encryption import (
    load_hippius_private_input_transport,
    prepare_hippius_private_input_transport,
)
from ditto.api_server.coding_hippius_probe import (
    HIPPIUS_REVIEWED_REVISION,
    HippiusProbeCheck,
    HippiusProbeCheckStatus,
    HippiusProbeCredential,
    HippiusProbeReceipt,
    load_hippius_probe_receipt,
    write_hippius_probe_receipt,
)
from ditto.api_server.coding_hippius_publication import (
    HippiusPrivateInputConflict,
    HippiusPrivateInputNotFound,
    HippiusPrivateInputPublicationConfig,
    HippiusPrivateInputPublicationError,
    HippiusPrivateInputPublicationStatus,
    hippius_private_input_remote_key,
    hippius_private_input_signing_message,
    load_curator_signing_public_key,
    publish_hippius_private_inputs,
    write_hippius_private_input_publication_receipt,
    write_hippius_private_input_signing_message,
)
from ditto.tests.api_server.test_coding_catalog_publication import (
    _write_fixture,
    _write_wrapping_public_key,
)

_ROOT = Path(__file__).resolve().parents[5]


def _load_script(name: str) -> ModuleType:
    path = _ROOT / f"apps/platform/scripts/{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _credential(name: str) -> HippiusProbeCredential:
    return HippiusProbeCredential(
        access_key=f"hip_{name}_access",
        secret_key=f"{name}-secret",
    )


def _config(**overrides: object) -> HippiusPrivateInputPublicationConfig:
    values: dict[str, object] = {
        "endpoint_url": "https://s3.hippius.com",
        "bucket": "coding-private-inputs",
        "curator": _credential("curator"),
        "reader": _credential("reader"),
        "region": "decentralized",
        "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return HippiusPrivateInputPublicationConfig(**values)  # type: ignore[arg-type]


class _FakePublicationTransport:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_count = 0

    async def get_object(self, *, key: str, max_bytes: int) -> bytes:
        try:
            body = self.objects[key]
        except KeyError as error:
            raise HippiusPrivateInputNotFound("missing") from error
        if len(body) > max_bytes:
            raise AssertionError("fake object exceeded requested bound")
        return body

    async def put_object(
        self,
        *,
        key: str,
        body: bytes,
        metadata: Mapping[str, str],
    ) -> None:
        assert metadata["ciphertext-sha256"] == hashlib.sha256(body).hexdigest()
        self.put_count += 1
        self.objects[key] = body


def _write_probe_receipt(
    root: Path, config: HippiusPrivateInputPublicationConfig
) -> Path:
    authority = config.authority_sha256
    receipt = HippiusProbeReceipt(
        schema="dittobench-coding-hippius-capability-probe-v1",
        source_sha="a" * 40,
        reviewed_hippius_revision=HIPPIUS_REVIEWED_REVISION,
        checked_at="2026-09-02T15:00:00Z",
        provider="hippius",
        private_input_authority_sha256=authority,
        sealed_evidence_authority_sha256="b" * 64,
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
    )
    path = (root / "probe-receipt.json").resolve()
    write_hippius_probe_receipt(receipt=receipt, output=path)
    return path


def _write_signed_transport(
    root: Path,
    config: HippiusPrivateInputPublicationConfig,
) -> tuple[Path, Path, Path, Path]:
    commitment_path, records_dir = _write_fixture(root)
    _wrapping_private, wrapping_public = _write_wrapping_public_key(root)
    transport_dir = (root / "encrypted").resolve()
    manifest = prepare_hippius_private_input_transport(
        commitment_path=commitment_path,
        records_dir=records_dir,
        wrapping_public_key_path=wrapping_public,
        output_dir=transport_dir,
    )
    probe_path = _write_probe_receipt(root, config)
    _probe, probe_payload_sha256 = load_hippius_probe_receipt(probe_path)
    signing_private = Ed25519PrivateKey.generate()
    public_path = root / "curator-signing-public.pem"
    public_path.write_bytes(
        signing_private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    _public, signing_key_sha256 = load_curator_signing_public_key(public_path)
    message = hippius_private_input_signing_message(
        manifest=manifest,
        probe_receipt_payload_sha256=probe_payload_sha256,
        private_input_authority_sha256=config.authority_sha256,
        curator_signing_key_sha256=signing_key_sha256,
    )
    signature_path = root / "curator-signature.bin"
    signature_path.write_bytes(signing_private.sign(message))
    return transport_dir, probe_path, public_path, signature_path


async def test_signed_hippius_publication_uploads_verifies_and_replays(
    tmp_path: Path,
) -> None:
    config = _config()
    transport_dir, probe_path, public_path, signature_path = _write_signed_transport(
        tmp_path, config
    )
    transport = _FakePublicationTransport()

    first = await publish_hippius_private_inputs(
        config=config,
        transport=transport,
        transport_dir=transport_dir,
        probe_receipt_path=probe_path,
        curator_public_key_path=public_path,
        curator_signature_path=signature_path,
        source_sha="c" * 40,
        now=datetime(2026, 9, 2, 16, 0, tzinfo=UTC),
    )

    assert first.ready is True
    assert first.weight_eligible is False
    assert len(first.objects) == 1
    assert first.objects[0].status is HippiusPrivateInputPublicationStatus.UPLOADED
    assert transport.put_count == 1
    manifest = load_hippius_private_input_transport(transport_dir)
    remote_key = hippius_private_input_remote_key(
        transport_manifest_sha256=manifest.transport_manifest_sha256,
        catalog_index=0,
    )
    assert (
        first.objects[0].remote_object_key_sha256
        == hashlib.sha256(remote_key.encode()).hexdigest()
    )
    _probe, probe_payload_sha256 = load_hippius_probe_receipt(probe_path)
    _public, signing_key_sha256 = load_curator_signing_public_key(public_path)
    signing_message = hippius_private_input_signing_message(
        manifest=manifest,
        probe_receipt_payload_sha256=probe_payload_sha256,
        private_input_authority_sha256=config.authority_sha256,
        curator_signing_key_sha256=signing_key_sha256,
    )
    signing_message_path = (tmp_path / "signing-message.bin").resolve()
    signing_message_sha256 = write_hippius_private_input_signing_message(
        message=signing_message,
        output=signing_message_path,
    )
    assert signing_message_path.read_bytes() == signing_message
    assert signing_message_sha256 == hashlib.sha256(signing_message).hexdigest()
    assert stat.S_IMODE(signing_message_path.stat().st_mode) == 0o600

    second = await publish_hippius_private_inputs(
        config=config,
        transport=transport,
        transport_dir=transport_dir,
        probe_receipt_path=probe_path,
        curator_public_key_path=public_path,
        curator_signature_path=signature_path,
        source_sha="c" * 40,
        now=datetime(2026, 9, 2, 16, 1, tzinfo=UTC),
    )
    assert second.objects[0].status is HippiusPrivateInputPublicationStatus.REUSED
    assert transport.put_count == 1

    receipt_path = (tmp_path / "publication-receipt.json").resolve()
    receipt_sha256 = write_hippius_private_input_publication_receipt(
        receipt=second,
        output=receipt_path,
    )
    assert len(receipt_sha256) == 64
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    raw = receipt_path.read_text()
    for sensitive in (
        config.endpoint_url,
        config.bucket,
        config.curator.access_key,
        config.curator.secret_key,
        config.reader.access_key,
        config.reader.secret_key,
        remote_key,
    ):
        assert sensitive not in raw


async def test_publication_rejects_probe_drift_signature_and_remote_conflict(
    tmp_path: Path,
) -> None:
    config = _config()
    transport_dir, probe_path, public_path, signature_path = _write_signed_transport(
        tmp_path, config
    )
    transport = _FakePublicationTransport()

    with pytest.raises(HippiusPrivateInputPublicationError, match="freshness"):
        await publish_hippius_private_inputs(
            config=config,
            transport=transport,
            transport_dir=transport_dir,
            probe_receipt_path=probe_path,
            curator_public_key_path=public_path,
            curator_signature_path=signature_path,
            source_sha="c" * 40,
            now=datetime(2026, 9, 4, 16, 0, tzinfo=UTC),
        )

    with pytest.raises(HippiusPrivateInputPublicationError, match="authority"):
        await publish_hippius_private_inputs(
            config=_config(bucket="another-private-inputs"),
            transport=transport,
            transport_dir=transport_dir,
            probe_receipt_path=probe_path,
            curator_public_key_path=public_path,
            curator_signature_path=signature_path,
            source_sha="c" * 40,
        )

    signature_path.write_bytes(b"x" * 64)
    with pytest.raises(HippiusPrivateInputPublicationError, match="signature"):
        await publish_hippius_private_inputs(
            config=config,
            transport=transport,
            transport_dir=transport_dir,
            probe_receipt_path=probe_path,
            curator_public_key_path=public_path,
            curator_signature_path=signature_path,
            source_sha="c" * 40,
        )

    # Restore a valid signature, then pre-position different bytes at the exact
    # manifest-derived key. Publication must not overwrite them.
    transport_dir, probe_path, public_path, signature_path = _write_signed_transport(
        tmp_path / "conflict", config
    )
    manifest = load_hippius_private_input_transport(transport_dir)
    remote_key = hippius_private_input_remote_key(
        transport_manifest_sha256=manifest.transport_manifest_sha256,
        catalog_index=0,
    )
    transport.objects[remote_key] = b"different"
    with pytest.raises(HippiusPrivateInputConflict):
        await publish_hippius_private_inputs(
            config=config,
            transport=transport,
            transport_dir=transport_dir,
            probe_receipt_path=probe_path,
            curator_public_key_path=public_path,
            curator_signature_path=signature_path,
            source_sha="c" * 40,
        )
    assert transport.put_count == 0


def test_publication_scripts_plan_message_and_gate_live_call(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    transport_dir, probe_path, public_path, _signature_path = _write_signed_transport(
        tmp_path, config
    )
    plan_script = _load_script("plan_hippius_private_input_signature")
    message_path = (tmp_path / "external-signing-message.bin").resolve()
    assert (
        plan_script.main(
            [
                "--transport-dir",
                str(transport_dir),
                "--probe-receipt",
                str(probe_path),
                "--curator-public-key",
                str(public_path),
                "--output",
                str(message_path),
            ]
        )
        == 0
    )
    assert message_path.is_file()
    assert stat.S_IMODE(message_path.stat().st_mode) == 0o600
    assert "message_sha256=" in capsys.readouterr().out

    publish_script = _load_script("publish_hippius_private_inputs")
    with pytest.raises(SystemExit) as caught:
        publish_script.main(
            [
                "--transport-dir",
                "missing",
                "--probe-receipt",
                "missing",
                "--curator-public-key",
                "missing",
                "--curator-signature",
                "missing",
                "--receipt-output",
                str((tmp_path / "not-created.json").resolve()),
                "--confirm",
                "PUBLISH CODING INPUTS",
            ]
        )
    assert caught.value.code == 2
    assert "PUBLISH HIPPIUS CODING PRIVATE INPUTS" in capsys.readouterr().err

    for name in tuple(os.environ):
        if name.startswith("DITTO_CODING_HIPPIUS_"):
            monkeypatch.delenv(name)
    assert (
        publish_script.main(
            [
                "--transport-dir",
                "missing",
                "--probe-receipt",
                "missing",
                "--curator-public-key",
                "missing",
                "--curator-signature",
                "missing",
                "--receipt-output",
                str((tmp_path / "still-not-created.json").resolve()),
                "--confirm",
                "PUBLISH HIPPIUS CODING PRIVATE INPUTS",
            ]
        )
        == 2
    )
    stderr = capsys.readouterr().err
    assert "required Hippius publication setting is missing" in stderr
    assert "Traceback" not in stderr
