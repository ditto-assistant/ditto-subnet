from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ditto.api_models.coding_artifacts import CodingArtifactDeliveryPhase
from ditto.api_models.coding_catalog import CodingCatalogCommitment
from ditto.api_server.coding_hippius_encryption import (
    HippiusPrivateInputEncryptionError,
    load_hippius_private_input_transport,
    load_hippius_private_input_transport_manifest,
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
    HippiusPrivateInputNotFound,
    HippiusPrivateInputPublicationConfig,
    hippius_private_input_signing_message,
    load_curator_signing_public_key,
    load_hippius_private_input_publication_receipt,
    publish_hippius_private_inputs,
    write_hippius_private_input_publication_receipt,
)
from ditto.api_server.coding_hippius_retrieval import (
    AiobotoHippiusPrivateInputReader,
    HippiusPrivateInputRetrievalConfig,
    HippiusPrivateInputRetrievalIntegrity,
    HippiusPrivateInputRetrievalUnavailable,
    HippiusPrivateInputRetriever,
    HippiusPrivateInputTicketAuthority,
    HippiusPrivateInputUnwrapRequest,
    HippiusPrivateInputUnwrapResult,
    _validate_presigned_get_url,
    parse_hippius_private_input_retrieval_config,
)
from ditto.tests.api_server.test_coding_catalog_publication import (
    _write_fixture,
    _write_wrapping_public_key,
)


def _credential(name: str) -> HippiusProbeCredential:
    return HippiusProbeCredential(
        access_key=f"hip_{name}_access",
        secret_key=f"{name}-secret",
    )


def _publication_config() -> HippiusPrivateInputPublicationConfig:
    return HippiusPrivateInputPublicationConfig(
        endpoint_url="https://s3.hippius.com",
        bucket="coding-private-inputs",
        curator=_credential("curator"),
        reader=_credential("reader"),
        region="decentralized",
        timeout_seconds=5.0,
    )


def _retrieval_config() -> HippiusPrivateInputRetrievalConfig:
    publication = _publication_config()
    return HippiusPrivateInputRetrievalConfig(
        endpoint_url=publication.endpoint_url,
        bucket=publication.bucket,
        curator_access_key_id=publication.curator.access_key,
        reader=publication.reader,
        region=publication.region,
        timeout_seconds=publication.timeout_seconds,
    )


class _ObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.requested_keys: list[str] = []

    async def get_object(self, *, key: str, max_bytes: int) -> bytes:
        self.requested_keys.append(key)
        try:
            body = self.objects[key]
        except KeyError as error:
            raise HippiusPrivateInputNotFound("missing") from error
        assert len(body) <= max_bytes
        return body

    async def put_object(
        self,
        *,
        key: str,
        body: bytes,
        metadata: Mapping[str, str],
    ) -> None:
        assert metadata["ciphertext-sha256"] == hashlib.sha256(body).hexdigest()
        self.objects[key] = body


class _PresigningClient:
    def __init__(self, url: str) -> None:
        self._url = url

    async def __aenter__(self) -> _PresigningClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def generate_presigned_url(self, *_args: object, **_kwargs: object) -> str:
        return self._url


class _PresigningSession:
    def __init__(self, url: str) -> None:
        self._url = url

    def client(self, *_args: object, **_kwargs: object) -> _PresigningClient:
        return _PresigningClient(self._url)


@dataclass
class _FakeUnwrapper:
    private_key: rsa.RSAPrivateKey
    mode: str = "valid"

    def __post_init__(self) -> None:
        self.requests: list[HippiusPrivateInputUnwrapRequest] = []

    async def unwrap_data_key(
        self, request: HippiusPrivateInputUnwrapRequest
    ) -> HippiusPrivateInputUnwrapResult:
        self.requests.append(request)
        data_key = self.private_key.decrypt(
            request.wrapped_data_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=bytes.fromhex(request.aad_sha256),
            ),
        )
        request_sha256 = request.request_sha256
        expires_at = request.ticket_deadline - timedelta(seconds=1)
        if self.mode == "wrong-request":
            request_sha256 = "f" * 64
        elif self.mode == "wrong-key":
            data_key = b"x" * 32
        elif self.mode == "late-expiry":
            expires_at = request.ticket_deadline + timedelta(seconds=1)
        return HippiusPrivateInputUnwrapResult(
            request_sha256=request_sha256,
            data_key=data_key,
            expires_at=expires_at,
        )


@dataclass(frozen=True)
class _Release:
    commitment: CodingCatalogCommitment
    manifest_path: Path
    receipt_path: Path
    curator_public_key_path: Path
    store: _ObjectStore
    wrapping_private_key: rsa.RSAPrivateKey
    transport_manifest_sha256: str
    receipt_payload_sha256: str


def _write_probe_receipt(
    root: Path,
    config: HippiusPrivateInputPublicationConfig,
) -> Path:
    path = (root / "probe-receipt.json").resolve()
    write_hippius_probe_receipt(
        receipt=HippiusProbeReceipt(
            schema="dittobench-coding-hippius-capability-probe-v1",
            source_sha="a" * 40,
            reviewed_hippius_revision=HIPPIUS_REVIEWED_REVISION,
            checked_at="2026-09-02T15:00:00Z",
            provider="hippius",
            private_input_authority_sha256=config.authority_sha256,
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
        ),
        output=path,
    )
    return path


async def _published_release(root: Path) -> _Release:
    config = _publication_config()
    commitment_path, records_dir = _write_fixture(root)
    wrapping_private, wrapping_public_path = _write_wrapping_public_key(root)
    transport_dir = (root / "transport").resolve()
    manifest = prepare_hippius_private_input_transport(
        commitment_path=commitment_path,
        records_dir=records_dir,
        wrapping_public_key_path=wrapping_public_path,
        output_dir=transport_dir,
    )
    probe_path = _write_probe_receipt(root, config)
    _probe, probe_payload_sha256 = load_hippius_probe_receipt(probe_path)
    signing_private = Ed25519PrivateKey.generate()
    curator_public_key_path = root / "curator-signing-public.pem"
    curator_public_key_path.write_bytes(
        signing_private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    _public_key, signing_key_sha256 = load_curator_signing_public_key(
        curator_public_key_path
    )
    signature_path = root / "curator-signature.bin"
    signature_path.write_bytes(
        signing_private.sign(
            hippius_private_input_signing_message(
                manifest=manifest,
                probe_receipt_payload_sha256=probe_payload_sha256,
                private_input_authority_sha256=config.authority_sha256,
                curator_signing_key_sha256=signing_key_sha256,
            )
        )
    )
    store = _ObjectStore()
    receipt = await publish_hippius_private_inputs(
        config=config,
        transport=store,
        transport_dir=transport_dir,
        probe_receipt_path=probe_path,
        curator_public_key_path=curator_public_key_path,
        curator_signature_path=signature_path,
        source_sha="c" * 40,
        now=datetime(2026, 9, 2, 16, 0, tzinfo=UTC),
    )
    receipt_path = (root / "publication-receipt.json").resolve()
    receipt_payload_sha256 = write_hippius_private_input_publication_receipt(
        receipt=receipt,
        output=receipt_path,
    )
    loaded_receipt, loaded_sha256 = load_hippius_private_input_publication_receipt(
        receipt_path
    )
    assert loaded_receipt == receipt
    assert loaded_sha256 == receipt_payload_sha256
    return _Release(
        commitment=CodingCatalogCommitment.model_validate_json(
            commitment_path.read_bytes()
        ),
        manifest_path=transport_dir / "manifest.json",
        receipt_path=receipt_path,
        curator_public_key_path=curator_public_key_path,
        store=store,
        wrapping_private_key=wrapping_private,
        transport_manifest_sha256=manifest.transport_manifest_sha256,
        receipt_payload_sha256=receipt_payload_sha256,
    )


def _ticket(
    release: _Release, **overrides: object
) -> HippiusPrivateInputTicketAuthority:
    values: dict[str, object] = {
        "ticket_id": UUID("11111111-1111-4111-8111-111111111111"),
        "run_row_id": UUID("22222222-2222-4222-8222-222222222222"),
        "validator_hotkey": "5" + "A" * 47,
        "coding_run_id": "coding-run-private-001",
        "assignment_sha256": "3" * 64,
        "run_manifest_sha256": "4" * 64,
        "ticket_deadline": datetime(2026, 9, 2, 17, 0, tzinfo=UTC),
        "delivery_phase": CodingArtifactDeliveryPhase.AUTHORING,
        "commitment": release.commitment,
        "catalog_index": 0,
        "transport_manifest_sha256": release.transport_manifest_sha256,
        "publication_receipt_payload_sha256": release.receipt_payload_sha256,
        "weight_eligible": False,
    }
    values.update(overrides)
    return HippiusPrivateInputTicketAuthority(**values)  # type: ignore[arg-type]


async def test_retrieval_fetches_one_exact_object_and_binds_unwrap_to_ticket(
    tmp_path: Path,
) -> None:
    release = await _published_release(tmp_path)
    unwrapper = _FakeUnwrapper(release.wrapping_private_key)
    retriever = HippiusPrivateInputRetriever(
        config=_retrieval_config(),
        manifest_path=release.manifest_path,
        publication_receipt_path=release.receipt_path,
        curator_public_key_path=release.curator_public_key_path,
        reader=release.store,
        unwrapper=unwrapper,
    )
    ticket = _ticket(release)

    record = await retriever.get_task_material(
        authority=ticket,
        now=datetime(2026, 9, 2, 16, 30, tzinfo=UTC),
    )

    assert record.catalog_commitment_sha256 == release.commitment.commitment_sha256
    assert record.task_version.payload.catalog_index == 0
    assert len(release.store.requested_keys) == 3  # publish preflight, verify, retrieve
    request = unwrapper.requests[0]
    assert request.ticket_id == ticket.ticket_id
    assert request.run_row_id == ticket.run_row_id
    assert request.assignment_sha256 == ticket.assignment_sha256
    assert request.run_manifest_sha256 == ticket.run_manifest_sha256
    assert request.delivery_phase is CodingArtifactDeliveryPhase.AUTHORING
    assert request.ticket_deadline == ticket.ticket_deadline
    assert request.catalog_commitment_sha256 == release.commitment.commitment_sha256
    assert "wrapped_data_key" not in repr(request)
    assert request.wrapped_data_key not in repr(request).encode()
    assert retriever.timeout_seconds == 5.0
    assert retriever.authority_sha256 == _retrieval_config().authority_sha256


@pytest.mark.parametrize("mode", ["wrong-request", "late-expiry", "wrong-key"])
async def test_retrieval_rejects_unwrap_drift(
    tmp_path: Path,
    mode: str,
) -> None:
    release = await _published_release(tmp_path)
    unwrapper = _FakeUnwrapper(release.wrapping_private_key, mode=mode)
    retriever = HippiusPrivateInputRetriever(
        config=_retrieval_config(),
        manifest_path=release.manifest_path,
        publication_receipt_path=release.receipt_path,
        curator_public_key_path=release.curator_public_key_path,
        reader=release.store,
        unwrapper=unwrapper,
    )

    with pytest.raises(HippiusPrivateInputRetrievalIntegrity):
        await retriever.get_task_material(
            authority=_ticket(release),
            now=datetime(2026, 9, 2, 16, 30, tzinfo=UTC),
        )


async def test_retrieval_rejects_ciphertext_ticket_and_registration_drift(
    tmp_path: Path,
) -> None:
    release = await _published_release(tmp_path)
    unwrapper = _FakeUnwrapper(release.wrapping_private_key)
    retriever = HippiusPrivateInputRetriever(
        config=_retrieval_config(),
        manifest_path=release.manifest_path,
        publication_receipt_path=release.receipt_path,
        curator_public_key_path=release.curator_public_key_path,
        reader=release.store,
        unwrapper=unwrapper,
    )
    remote_key = next(iter(release.store.objects))
    release.store.objects[remote_key] = b"x" * len(release.store.objects[remote_key])
    with pytest.raises(HippiusPrivateInputRetrievalIntegrity, match="ciphertext"):
        await retriever.get_task_material(
            authority=_ticket(release),
            now=datetime(2026, 9, 2, 16, 30, tzinfo=UTC),
        )
    assert not unwrapper.requests

    with pytest.raises(HippiusPrivateInputRetrievalUnavailable, match="active"):
        await retriever.get_task_material(
            authority=_ticket(release),
            now=datetime(2026, 9, 2, 17, 0, tzinfo=UTC),
        )
    with pytest.raises(HippiusPrivateInputRetrievalIntegrity, match="release"):
        await retriever.get_task_material(
            authority=_ticket(release, transport_manifest_sha256="f" * 64),
            now=datetime(2026, 9, 2, 16, 30, tzinfo=UTC),
        )


async def test_retriever_rejects_receipt_and_runtime_authority_drift(
    tmp_path: Path,
) -> None:
    release = await _published_release(tmp_path)
    raw = json.loads(release.receipt_path.read_text())
    raw["catalog_commitment_sha256"] = "f" * 64
    release.receipt_path.write_text(json.dumps(raw))
    with pytest.raises(HippiusPrivateInputRetrievalIntegrity, match="registered"):
        HippiusPrivateInputRetriever(
            config=_retrieval_config(),
            manifest_path=release.manifest_path,
            publication_receipt_path=release.receipt_path,
            curator_public_key_path=release.curator_public_key_path,
            reader=release.store,
            unwrapper=_FakeUnwrapper(release.wrapping_private_key),
        )

    second = await _published_release(tmp_path / "second")
    drifted = _retrieval_config()
    drifted = HippiusPrivateInputRetrievalConfig(
        endpoint_url=drifted.endpoint_url,
        bucket="different-private-inputs",
        curator_access_key_id=drifted.curator_access_key_id,
        reader=drifted.reader,
        region=drifted.region,
        timeout_seconds=drifted.timeout_seconds,
    )
    with pytest.raises(HippiusPrivateInputRetrievalIntegrity, match="registered"):
        HippiusPrivateInputRetriever(
            config=drifted,
            manifest_path=second.manifest_path,
            publication_receipt_path=second.receipt_path,
            curator_public_key_path=second.curator_public_key_path,
            reader=second.store,
            unwrapper=_FakeUnwrapper(second.wrapping_private_key),
        )


def test_retrieval_config_and_presigned_url_are_fail_closed() -> None:
    config = _retrieval_config()
    loaded = parse_hippius_private_input_retrieval_config(
        {
            "DITTO_CODING_HIPPIUS_ENDPOINT_URL": config.endpoint_url,
            "DITTO_CODING_HIPPIUS_REGION": config.region,
            "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_BUCKET": config.bucket,
            "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY": (
                config.curator_access_key_id
            ),
            "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY": (
                config.reader.access_key
            ),
            "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY": (
                config.reader.secret_key
            ),
        }
    )
    assert loaded.authority_sha256 == config.authority_sha256
    for secret in (
        config.endpoint_url,
        config.bucket,
        config.curator_access_key_id,
        config.reader.access_key,
        config.reader.secret_key,
    ):
        assert secret not in repr(config)
    key = "coding-private-inputs/v1/" + "a" * 64 + "/objects/000000.bin"
    _validate_presigned_get_url(
        config=config,
        key=key,
        url=(f"https://s3.hippius.com/coding-private-inputs/{key}?X-Amz-Signature=abc"),
    )
    with pytest.raises(HippiusPrivateInputRetrievalIntegrity, match="origin"):
        _validate_presigned_get_url(
            config=config,
            key=key,
            url=f"https://evil.example/{config.bucket}/{key}?X-Amz-Signature=abc",
        )
    with pytest.raises(HippiusPrivateInputRetrievalIntegrity, match="configuration"):
        HippiusPrivateInputRetrievalConfig(
            endpoint_url="https://evil.example",
            bucket=config.bucket,
            curator_access_key_id=config.curator_access_key_id,
            reader=config.reader,
        )


async def test_live_reader_uses_one_bounded_nonredirecting_exact_get() -> None:
    config = _retrieval_config()
    key = "coding-private-inputs/v1/" + "a" * 64 + "/objects/000000.bin"
    url = f"https://s3.hippius.com/{config.bucket}/{key}?X-Amz-Signature=abc"
    requests: list[httpx.Request] = []

    def success(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"x" * 17)

    reader = AiobotoHippiusPrivateInputReader(
        config,
        http_transport=httpx.MockTransport(success),
    )
    reader._session = _PresigningSession(url)  # type: ignore[assignment]
    try:
        assert await reader.get_object(key=key, max_bytes=17) == b"x" * 17
    finally:
        await reader.__aexit__(None, None, None)
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.host == "s3.hippius.com"
    with pytest.raises(HippiusPrivateInputRetrievalIntegrity, match="exact-read"):
        await reader.get_object(key="../secret", max_bytes=17)
    with pytest.raises(HippiusPrivateInputRetrievalIntegrity, match="exact-read"):
        await reader.get_object(key=key + ".bak", max_bytes=17)

    def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/leak"})

    reader = AiobotoHippiusPrivateInputReader(
        config,
        http_transport=httpx.MockTransport(redirect),
    )
    reader._session = _PresigningSession(url)  # type: ignore[assignment]
    try:
        with pytest.raises(HippiusPrivateInputRetrievalIntegrity, match="redirect"):
            await reader.get_object(key=key, max_bytes=17)
    finally:
        await reader.__aexit__(None, None, None)


def test_manifest_only_loader_does_not_require_local_ciphertext(tmp_path: Path) -> None:
    commitment_path, records_dir = _write_fixture(tmp_path)
    _private, public_path = _write_wrapping_public_key(tmp_path)
    directory = (tmp_path / "transport").resolve()
    expected = prepare_hippius_private_input_transport(
        commitment_path=commitment_path,
        records_dir=records_dir,
        wrapping_public_key_path=public_path,
        output_dir=directory,
    )
    ciphertext_path = directory / expected.objects[0].ciphertext_relative_path
    ciphertext_path.unlink()
    assert (
        load_hippius_private_input_transport_manifest(directory / "manifest.json")
        == expected
    )
    with pytest.raises(HippiusPrivateInputEncryptionError):
        load_hippius_private_input_transport(directory)
