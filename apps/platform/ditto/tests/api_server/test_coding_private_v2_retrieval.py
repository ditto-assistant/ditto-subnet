from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_private_v2_registry import (
    CodingPrivateV2RegistrationAuthority,
)
from ditto.api_server.coding_hippius_publication import (
    HippiusPrivateInputPublicationStatus,
)
from ditto.api_server.coding_private_v2_publication import (
    PrivateV2PublicationObject,
    PrivateV2PublicationReceipt,
    private_v2_publication_signing_message,
    private_v2_remote_object_key,
    write_private_v2_publication_receipt,
)
from ditto.api_server.coding_private_v2_retrieval import (
    PrivateV2InputRetriever,
    PrivateV2ObjectGrant,
    PrivateV2RetrievalError,
    PrivateV2UnwrapRequest,
    PrivateV2UnwrapResult,
)

NOW = 1788590000
PLAIN = b"PRIVATE_MARKER: synthetic task input"
KEY = b"k" * 32


def _bytes(value: dict[str, Any]) -> bytes:
    return coding_canonical_json_bytes(value, maximum_bytes=16 << 20, label="fixture")


def _sha(value: dict[str, Any]) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


class Grants:
    def __init__(self, grant: PrivateV2ObjectGrant):
        self.grant: PrivateV2ObjectGrant | None = grant

    async def active_grant(
        self, *, grant_id: UUID, audience: str
    ) -> PrivateV2ObjectGrant | None:
        if (
            self.grant is not None
            and self.grant.grant_id == grant_id
            and self.grant.audience == audience
        ):
            return self.grant
        return None


class Reader:
    def __init__(self, ciphertext: bytes):
        self.ciphertext = ciphertext
        self.calls = 0
        self.revoke: Grants | None = None

    async def get_object(self, *, key: str, max_bytes: int) -> bytes:
        assert key.startswith("coding-private-inputs/v2/")
        assert max_bytes == len(self.ciphertext)
        self.calls += 1
        if self.revoke is not None:
            self.revoke.grant = None
        return self.ciphertext


class Unwrapper:
    def __init__(self):
        self.calls = 0
        self.wrong_request = False
        self.revoke: Grants | None = None
        self.hang = False

    async def unwrap(self, request: PrivateV2UnwrapRequest) -> PrivateV2UnwrapResult:
        self.calls += 1
        if self.hang:
            await asyncio.Event().wait()
        if self.revoke is not None:
            self.revoke.grant = None
        return PrivateV2UnwrapResult(
            "0" * 64 if self.wrong_request else request.digest(), KEY
        )


def _fixture(
    tmp_path: Path,
    *,
    phase: Literal["authoring", "grading"] = "authoring",
    tamper: Literal["payload", "signature", "curator"] | None = None,
) -> tuple[PrivateV2InputRetriever, Grants, Reader, Unwrapper]:
    tmp_path.chmod(0o700)
    digest = hashlib.sha256(PLAIN).hexdigest()
    payload = {
        "schema": "dittobench-coding-private-payload-v2",
        "coding_contract_version": 2,
        "weight_eligible": False,
        "catalog_sha256": "1" * 64,
        "catalog_merkle_root": "2" * 64,
        "task_version_count": 250,
        "objects": [{"sha256": digest, "size_bytes": len(PLAIN)}],
        "task_assets": [
            {
                "catalog_index": i,
                "task_version_id": f"synthetic-{i}",
                "task_commitment_sha256": "3" * 64,
                "artifacts": dict.fromkeys(
                    (
                        "catalog_record",
                        "issue",
                        "visible_bundle",
                        "memory_bundle",
                        "runtime_policy",
                        "resource_profile",
                        "grader_bundle",
                    ),
                    digest,
                ),
            }
            for i in range(250)
        ],
    }
    payload["payload_sha256"] = _sha(payload)
    aad = _bytes(
        {
            "schema": "dittobench-coding-private-v2-transport-aad-v1",
            "payload_sha256": payload["payload_sha256"],
            "catalog_sha256": "1" * 64,
            "plaintext_sha256": digest,
            "plaintext_size_bytes": len(PLAIN),
            "wrapping_key_sha256": "4" * 64,
        }
    )
    ciphertext = AESGCM(KEY).encrypt(b"n" * 12, PLAIN, aad)
    manifest = {
        "schema": "dittobench-coding-private-v2-transport-v1",
        "coding_contract_version": 2,
        "weight_eligible": False,
        "payload_sha256": payload["payload_sha256"],
        "catalog_sha256": "1" * 64,
        "catalog_merkle_root": "2" * 64,
        "wrapping_key_sha256": "4" * 64,
        "objects": [
            {
                "plaintext_sha256": digest,
                "plaintext_size_bytes": len(PLAIN),
                "ciphertext_relative_path": f"objects/{digest}.bin",
                "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
                "ciphertext_size_bytes": len(ciphertext),
                "nonce_b64": base64.b64encode(b"n" * 12).decode(),
                "wrapped_data_key_b64": base64.b64encode(b"w" * 384).decode(),
                "aad_sha256": hashlib.sha256(aad).hexdigest(),
            }
        ],
    }
    manifest["transport_sha256"] = _sha(manifest)
    if tamper == "payload":
        payload["task_version_count"] = 249
    (tmp_path / "transport.json").write_bytes(_bytes(manifest))
    (tmp_path / "payload.json").write_bytes(_bytes(payload))
    signer = Ed25519PrivateKey.generate()
    signer_sha = hashlib.sha256(
        signer.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).hexdigest()
    signature = signer.sign(
        private_v2_publication_signing_message(
            manifest=manifest,
            source_sha="a" * 40,
            probe_receipt_payload_sha256="5" * 64,
            private_input_authority_sha256="6" * 64,
            curator_signing_key_sha256=signer_sha,
        )
    )
    if tamper == "signature":
        signature = b"\x00" * 64
    remote = private_v2_remote_object_key(
        transport_sha256=str(manifest["transport_sha256"]), object_index=0
    )
    receipt = PrivateV2PublicationReceipt(
        schema="dittobench-coding-private-v2-publication-v1",
        source_sha="a" * 40,
        checked_at="2026-09-05T00:00:00Z",
        provider="hippius",
        probe_receipt_payload_sha256="5" * 64,
        private_input_authority_sha256="6" * 64,
        transport_sha256=str(manifest["transport_sha256"]),
        payload_sha256=str(payload["payload_sha256"]),
        catalog_sha256="1" * 64,
        catalog_merkle_root="2" * 64,
        wrapping_key_sha256="4" * 64,
        curator_signing_key_sha256=signer_sha,
        curator_signature_b64=base64.b64encode(signature).decode(),
        object_count=1,
        objects=(
            PrivateV2PublicationObject(
                0,
                hashlib.sha256(remote.encode()).hexdigest(),
                hashlib.sha256(ciphertext).hexdigest(),
                len(ciphertext),
                HippiusPrivateInputPublicationStatus.UPLOADED,
            ),
        ),
        ready=True,
        shadow_only=True,
        weight_eligible=False,
    )
    receipt_sha = write_private_v2_publication_receipt(
        receipt=receipt, output=tmp_path / "receipt.json"
    )
    registration = {
        "schema": "dittobench-coding-private-v2-registration-v1",
        "coding_contract_version": 2,
        "weight_eligible": False,
        "shadow_only": True,
        "corpus_release_id": "synthetic-release",
        "private_release_sha256": "7" * 64,
        "catalog_sha256": "1" * 64,
        "catalog_merkle_root": "2" * 64,
        "payload_sha256": payload["payload_sha256"],
        "transport_sha256": manifest["transport_sha256"],
        "wrapping_key_sha256": "4" * 64,
        "publication_receipt_sha256": receipt_sha,
        "previous_registration_sha256": None,
    }
    registration["registration_sha256"] = _sha(registration)
    model = CodingPrivateV2RegistrationAuthority.model_validate(registration)
    grant = PrivateV2ObjectGrant(
        UUID(int=1),
        UUID(int=2),
        UUID(int=3),
        model.registration_sha256,
        0,
        phase,
        "platform-authoring" if phase == "authoring" else "platform-grading",
        ("issue", "grader_bundle"),
        NOW + 60,
        None,
    )
    grants, reader, unwrapper = Grants(grant), Reader(ciphertext), Unwrapper()
    retriever = PrivateV2InputRetriever(
        registration=model,
        transport_manifest=tmp_path / "transport.json",
        payload_authority=tmp_path / "payload.json",
        publication_receipt=tmp_path / "receipt.json",
        trusted_curator=(
            Ed25519PrivateKey.generate().public_key()
            if tamper == "curator"
            else signer.public_key()
        ),
        reader_authority_sha256="6" * 64,
        audience="platform-authoring" if phase == "authoring" else "platform-grading",
        grants=grants,
        reader=reader,
        unwrapper=unwrapper,
        clock=lambda: NOW,
    )
    return retriever, grants, reader, unwrapper


@pytest.mark.parametrize("tamper", ["payload", "signature", "curator"])
def test_authority_tampering_cannot_construct_retriever(
    tmp_path: Path, tamper: Literal["payload", "signature", "curator"]
) -> None:
    with pytest.raises(PrivateV2RetrievalError) as caught:
        _fixture(tmp_path, tamper=tamper)
    assert str(caught.value) == "private v2 retrieval authorities are invalid"
    assert caught.value.__suppress_context__


async def test_exact_object_roundtrip_needs_no_local_ciphertext_copy(
    tmp_path: Path,
) -> None:
    retriever, grants, reader, unwrapper = _fixture(tmp_path)
    assert await retriever.read(grant_id=UUID(int=1), role="issue") == PLAIN
    assert reader.calls == unwrapper.calls == 1
    assert not (tmp_path / "objects").exists()
    assert "PRIVATE_MARKER" not in repr(grants.grant)


async def test_grading_requires_patch_freeze(tmp_path: Path) -> None:
    retriever, grants, reader, unwrapper = _fixture(tmp_path, phase="grading")
    with pytest.raises(PrivateV2RetrievalError):
        await retriever.read(grant_id=UUID(int=1), role="grader_bundle")
    assert reader.calls == unwrapper.calls == 0
    assert grants.grant is not None
    grants.grant = replace(grants.grant, frozen_patch_sha256="f" * 64)
    assert await retriever.read(grant_id=UUID(int=1), role="grader_bundle") == PLAIN
    with pytest.raises(PrivateV2RetrievalError):
        await retriever.read(grant_id=UUID(int=999), role="grader_bundle")
    assert reader.calls == unwrapper.calls == 1


@pytest.mark.parametrize("role", ["grader_bundle", "other-task", "../secret"])
async def test_authoring_cannot_fetch_hidden_or_arbitrary_roles(
    tmp_path: Path, role: str
) -> None:
    retriever, _, reader, unwrapper = _fixture(tmp_path)
    with pytest.raises(PrivateV2RetrievalError):
        await retriever.read(grant_id=UUID(int=1), role=role)
    assert reader.calls == unwrapper.calls == 0


async def test_revoked_or_expired_grant_prevents_unwrap(tmp_path: Path) -> None:
    retriever, grants, reader, unwrapper = _fixture(tmp_path)
    assert grants.grant is not None
    original = grants.grant
    grants.grant = replace(original, expires_at_unix=NOW)
    with pytest.raises(PrivateV2RetrievalError):
        await retriever.read(grant_id=UUID(int=1), role="issue")
    assert reader.calls == unwrapper.calls == 0
    grants.grant = original
    reader.revoke = grants
    with pytest.raises(PrivateV2RetrievalError):
        await retriever.read(grant_id=UUID(int=1), role="issue")
    assert reader.calls == 1 and unwrapper.calls == 0


async def test_ciphertext_and_unwrap_binding_fail_safely(tmp_path: Path) -> None:
    retriever, _, reader, unwrapper = _fixture(tmp_path)
    original = reader.ciphertext
    reader.ciphertext = b"x" * len(original)
    with pytest.raises(PrivateV2RetrievalError) as caught:
        await retriever.read(grant_id=UUID(int=1), role="issue")
    assert "PRIVATE_MARKER" not in str(caught.value)
    assert unwrapper.calls == 0
    reader.ciphertext = original
    unwrapper.wrong_request = True
    with pytest.raises(PrivateV2RetrievalError):
        await retriever.read(grant_id=UUID(int=1), role="issue")


async def test_revocation_during_unwrap_prevents_plaintext_return(
    tmp_path: Path,
) -> None:
    retriever, grants, _, unwrapper = _fixture(tmp_path)
    unwrapper.revoke = grants
    with pytest.raises(PrivateV2RetrievalError):
        await retriever.read(grant_id=UUID(int=1), role="issue")
    assert unwrapper.calls == 1


async def test_unwrap_has_bounded_wall_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retriever, _, _, unwrapper = _fixture(tmp_path)
    unwrapper.hang = True
    monkeypatch.setattr(
        "ditto.api_server.coding_private_v2_retrieval.PRIVATE_V2_RETRIEVAL_TIMEOUT_SECONDS",
        0.01,
    )
    with pytest.raises(PrivateV2RetrievalError):
        await retriever.read(grant_id=UUID(int=1), role="issue")
    assert unwrapper.calls == 1
