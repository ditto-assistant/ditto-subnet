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

from ditto.api_server.coding_hippius_probe import load_hippius_probe_receipt
from ditto.api_server.coding_hippius_publication import (
    HippiusPrivateInputConflict,
    HippiusPrivateInputNotFound,
    HippiusPrivateInputPublicationConfig,
    HippiusPrivateInputPublicationStatus,
    load_curator_signing_public_key,
)
from ditto.api_server.coding_private_v2_publication import (
    PRIVATE_V2_PUBLICATION_MAX_CIPHERTEXT_BYTES,
    PrivateV2PublicationError,
    load_private_v2_publication_receipt,
    private_v2_publication_signing_message,
    private_v2_remote_object_key,
    publish_private_v2_to_hippius,
    write_private_v2_publication_receipt,
    write_private_v2_publication_signing_message,
)
from ditto.api_server.coding_private_v2_transport import (
    verify_private_v2_transport,
)
from ditto.tests.api_server.test_coding_hippius_publication import (
    _config,
    _write_probe_receipt,
)
from ditto.tests.api_server.test_coding_private_v2_transport import (
    _write_transport,
)

_ROOT = Path(__file__).resolve().parents[5]


def _load_script(name: str) -> ModuleType:
    path = _ROOT / f"apps/platform/scripts/{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeTransport:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_count = 0
        self.metadata: list[Mapping[str, str]] = []

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
        assert set(metadata) == {
            "ciphertext-sha256",
            "object-index",
            "transport-sha256",
        }
        self.put_count += 1
        self.metadata.append(metadata)
        self.objects[key] = body


def _write_signed_v2_transport(
    root: Path,
    config: HippiusPrivateInputPublicationConfig,
    *,
    source_sha: str = "c" * 40,
    ciphertext: bytes = b"\x00" * 17,
) -> tuple[Path, Path, Path, Path]:
    transport_directory = root / "transport"
    _write_transport(
        transport_directory,
        digest="1" * 64,
        ciphertext=ciphertext,
    )
    probe_path = _write_probe_receipt(root, config)
    probe, probe_sha256 = load_hippius_probe_receipt(probe_path)
    signing_private = Ed25519PrivateKey.generate()
    public_path = root / "curator-signing-public.pem"
    public_path.write_bytes(
        signing_private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    _public, signing_key_sha256 = load_curator_signing_public_key(public_path)
    manifest = verify_private_v2_transport(transport_directory)
    message = private_v2_publication_signing_message(
        manifest=manifest,
        source_sha=source_sha,
        probe_receipt_payload_sha256=probe_sha256,
        private_input_authority_sha256=probe.private_input_authority_sha256,
        curator_signing_key_sha256=signing_key_sha256,
    )
    signature_path = root / "curator-signature.bin"
    signature_path.write_bytes(signing_private.sign(message))
    return transport_directory, probe_path, public_path, signature_path


@pytest.mark.asyncio
async def test_private_v2_publication_uploads_verifies_replays_and_redacts(
    tmp_path: Path,
) -> None:
    config = _config()
    transport_directory, probe_path, public_path, signature_path = (
        _write_signed_v2_transport(tmp_path, config)
    )
    transport = _FakeTransport()
    valid_now = datetime(2026, 9, 2, 16, 0, tzinfo=UTC)

    first = await publish_private_v2_to_hippius(
        config=config,
        transport=transport,
        transport_directory=transport_directory,
        probe_receipt_path=probe_path,
        curator_public_key_path=public_path,
        curator_signature_path=signature_path,
        source_sha="c" * 40,
        now=valid_now,
    )

    assert first.ready is True
    assert first.shadow_only is True
    assert first.weight_eligible is False
    assert first.object_count == 1
    assert first.objects[0].status is HippiusPrivateInputPublicationStatus.UPLOADED
    assert transport.put_count == 1
    manifest = verify_private_v2_transport(transport_directory)
    remote_key = private_v2_remote_object_key(
        transport_sha256=manifest["transport_sha256"], object_index=0
    )
    assert (
        first.objects[0].remote_object_key_sha256
        == hashlib.sha256(remote_key.encode()).hexdigest()
    )

    second = await publish_private_v2_to_hippius(
        config=config,
        transport=transport,
        transport_directory=transport_directory,
        probe_receipt_path=probe_path,
        curator_public_key_path=public_path,
        curator_signature_path=signature_path,
        source_sha="c" * 40,
        now=datetime(2026, 9, 2, 16, 1, tzinfo=UTC),
    )
    assert second.objects[0].status is HippiusPrivateInputPublicationStatus.REUSED
    assert transport.put_count == 1

    receipt_path = (tmp_path / "publication-receipt.json").resolve()
    receipt_sha256 = write_private_v2_publication_receipt(
        receipt=second, output=receipt_path
    )
    loaded, loaded_sha256 = load_private_v2_publication_receipt(receipt_path)
    assert loaded == second
    assert loaded_sha256 == receipt_sha256
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
        "1" * 64,
    ):
        assert sensitive not in raw


@pytest.mark.asyncio
async def test_private_v2_publication_rejects_stale_probe_drift_and_conflict(
    tmp_path: Path,
) -> None:
    config = _config()
    transport_directory, probe_path, public_path, signature_path = (
        _write_signed_v2_transport(tmp_path, config)
    )
    transport = _FakeTransport()
    valid_now = datetime(2026, 9, 2, 16, 0, tzinfo=UTC)

    with pytest.raises(PrivateV2PublicationError, match="freshness"):
        await publish_private_v2_to_hippius(
            config=config,
            transport=transport,
            transport_directory=transport_directory,
            probe_receipt_path=probe_path,
            curator_public_key_path=public_path,
            curator_signature_path=signature_path,
            source_sha="c" * 40,
            now=datetime(2026, 9, 4, 16, 0, tzinfo=UTC),
        )

    with pytest.raises(PrivateV2PublicationError, match="authority"):
        await publish_private_v2_to_hippius(
            config=_config(bucket="another-private-inputs"),
            transport=transport,
            transport_directory=transport_directory,
            probe_receipt_path=probe_path,
            curator_public_key_path=public_path,
            curator_signature_path=signature_path,
            source_sha="c" * 40,
            now=valid_now,
        )

    signature_path.write_bytes(b"x" * 64)
    with pytest.raises(PrivateV2PublicationError, match="signature"):
        await publish_private_v2_to_hippius(
            config=config,
            transport=transport,
            transport_directory=transport_directory,
            probe_receipt_path=probe_path,
            curator_public_key_path=public_path,
            curator_signature_path=signature_path,
            source_sha="c" * 40,
            now=valid_now,
        )

    conflict_root = tmp_path / "conflict"
    conflict_root.mkdir(mode=0o700)
    transport_directory, probe_path, public_path, signature_path = (
        _write_signed_v2_transport(conflict_root, config)
    )
    manifest = verify_private_v2_transport(transport_directory)
    remote_key = private_v2_remote_object_key(
        transport_sha256=manifest["transport_sha256"], object_index=0
    )
    transport.objects[remote_key] = b"different"
    with pytest.raises(HippiusPrivateInputConflict):
        await publish_private_v2_to_hippius(
            config=config,
            transport=transport,
            transport_directory=transport_directory,
            probe_receipt_path=probe_path,
            curator_public_key_path=public_path,
            curator_signature_path=signature_path,
            source_sha="c" * 40,
            now=valid_now,
        )
    assert transport.put_count == 0


@pytest.mark.asyncio
async def test_private_v2_publication_rejects_unreviewed_large_object_before_io(
    tmp_path: Path,
) -> None:
    config = _config()
    ciphertext = b"x" * (PRIVATE_V2_PUBLICATION_MAX_CIPHERTEXT_BYTES + 1)
    transport_directory, probe_path, public_path, signature_path = (
        _write_signed_v2_transport(tmp_path, config, ciphertext=ciphertext)
    )
    transport = _FakeTransport()
    with pytest.raises(PrivateV2PublicationError, match="provider profile"):
        await publish_private_v2_to_hippius(
            config=config,
            transport=transport,
            transport_directory=transport_directory,
            probe_receipt_path=probe_path,
            curator_public_key_path=public_path,
            curator_signature_path=signature_path,
            source_sha="c" * 40,
            now=datetime(2026, 9, 2, 16, 0, tzinfo=UTC),
        )
    assert transport.put_count == 0
    assert transport.objects == {}


def test_private_v2_publication_scripts_plan_and_gate_provider_call(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    transport_directory, probe_path, public_path, _signature_path = (
        _write_signed_v2_transport(tmp_path, config)
    )
    plan_script = _load_script("plan_private_v2_publication_signature")
    message_path = (tmp_path / "signing-message.bin").resolve()
    assert (
        plan_script.main(
            [
                "--transport",
                str(transport_directory),
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

    publish_script = _load_script("publish_private_v2_to_hippius")
    with pytest.raises(SystemExit) as caught:
        publish_script.main(
            [
                "--transport",
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
                "PUBLISH PRIVATE V2",
            ]
        )
    assert caught.value.code == 2
    assert "PUBLISH HIPPIUS CODING PRIVATE V2 PAYLOAD" in capsys.readouterr().err

    for name in tuple(os.environ):
        if name.startswith("DITTO_CODING_HIPPIUS_"):
            monkeypatch.delenv(name)
    assert (
        publish_script.main(
            [
                "--transport",
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
                "PUBLISH HIPPIUS CODING PRIVATE V2 PAYLOAD",
            ]
        )
        == 2
    )
    stderr = capsys.readouterr().err
    assert "required Hippius publication setting is missing" in stderr
    assert "Traceback" not in stderr


def test_private_v2_signing_message_and_remote_identity_are_strict(
    tmp_path: Path,
) -> None:
    config = _config()
    transport_directory, probe_path, public_path, _signature_path = (
        _write_signed_v2_transport(tmp_path, config)
    )
    manifest = verify_private_v2_transport(transport_directory)
    probe, probe_sha256 = load_hippius_probe_receipt(probe_path)
    _public, signing_key_sha256 = load_curator_signing_public_key(public_path)
    message = private_v2_publication_signing_message(
        manifest=manifest,
        source_sha="c" * 40,
        probe_receipt_payload_sha256=probe_sha256,
        private_input_authority_sha256=probe.private_input_authority_sha256,
        curator_signing_key_sha256=signing_key_sha256,
    )
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    message_path = (protected / "message.bin").resolve()
    digest = write_private_v2_publication_signing_message(
        message=message, output=message_path
    )
    assert digest == hashlib.sha256(message).hexdigest()
    with pytest.raises(PrivateV2PublicationError, match="identity"):
        private_v2_remote_object_key(
            transport_sha256=manifest["transport_sha256"], object_index=True
        )
    with pytest.raises(PrivateV2PublicationError, match="message is invalid"):
        write_private_v2_publication_signing_message(
            message=b"", output=(protected / "empty.bin").resolve()
        )

    exposed = tmp_path / "exposed"
    exposed.mkdir(mode=0o755)
    with pytest.raises(PrivateV2PublicationError, match="protected directory"):
        write_private_v2_publication_signing_message(
            message=message, output=(exposed / "message.bin").resolve()
        )
