"""Platform-only, grant-bound retrieval of encrypted private v2 task objects."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import stat
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_private_v2_registry import (
    CodingPrivateV2RegistrationAuthority,
)
from ditto.api_server.coding_hippius_publication import load_curator_signing_public_key
from ditto.api_server.coding_hippius_retrieval import HippiusPrivateInputReader
from ditto.api_server.coding_private_v2_publication import (
    PRIVATE_V2_PUBLICATION_MAX_CIPHERTEXT_BYTES,
    load_private_v2_publication_receipt,
    private_v2_publication_signing_message,
    private_v2_remote_object_key,
)
from ditto.api_server.coding_private_v2_transport import (
    load_private_v2_transport_manifest,
)
from ditto.coding_hosted_private import AUTHORING_ROLES, GRADING_ROLES
from ditto.coding_hosted_private import PrivateV2ObjectGrant as PrivateV2ObjectGrant

_AUTHORING_ROLES = frozenset(AUTHORING_ROLES)
_GRADING_ROLES = frozenset(GRADING_ROLES)
_ALL_ROLES = _AUTHORING_ROLES | _GRADING_ROLES
PRIVATE_V2_RETRIEVAL_TIMEOUT_SECONDS = 30


class PrivateV2RetrievalError(ValueError):
    """Safe failure without object identities, keys or private body bytes."""


class PrivateV2GrantStore(Protocol):
    async def active_grant(
        self, *, grant_id: UUID, audience: str
    ) -> PrivateV2ObjectGrant | None:
        """Consult durable grant, attempt phase and current release lifecycle."""
        ...


@dataclass(frozen=True, repr=False)
class PrivateV2UnwrapRequest:
    schema: Literal["dittobench-coding-private-v2-unwrap-v1"]
    grant_id: str
    evaluation_id: str
    attempt_id: str
    registration_sha256: str
    transport_sha256: str
    plaintext_sha256: str
    ciphertext_sha256: str
    wrapping_key_sha256: str
    wrapped_data_key_b64: str
    aad_sha256: str
    phase: str
    role: str
    audience: str
    expires_at_unix: int
    frozen_patch_sha256: str | None

    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, repr=False)
class PrivateV2UnwrapResult:
    request_sha256: str
    data_key: bytes


class PrivateV2Unwrapper(Protocol):
    async def unwrap(
        self, request: PrivateV2UnwrapRequest
    ) -> PrivateV2UnwrapResult: ...


class PrivateV2InputRetriever:
    """Trusted service primitive; no constructor or route is enabled by default."""

    def __init__(
        self,
        *,
        registration: CodingPrivateV2RegistrationAuthority,
        transport_manifest: Path,
        payload_authority: Path,
        publication_receipt: Path,
        trusted_curator_public_key_path: Path,
        reader_authority_sha256: str,
        audience: Literal["platform-authoring", "platform-grading"],
        grants: PrivateV2GrantStore,
        reader: HippiusPrivateInputReader,
        unwrapper: PrivateV2Unwrapper,
        clock: Callable[[], int] | None = None,
    ) -> None:
        try:
            if audience not in {"platform-authoring", "platform-grading"}:
                raise ValueError("audience")
            registration = CodingPrivateV2RegistrationAuthority.model_validate(
                registration.model_dump(mode="json", by_alias=True)
            )
            manifest = load_private_v2_transport_manifest(transport_manifest)
            payload = _load_authority(payload_authority)
            trusted_curator, curator_sha = load_curator_signing_public_key(
                trusted_curator_public_key_path
            )
            receipt, receipt_sha = load_private_v2_publication_receipt(
                publication_receipt,
                curator_public_key_path=trusted_curator_public_key_path,
            )
            if (
                registration.publication_receipt_sha256 != receipt_sha
                or receipt.curator_signing_key_sha256 != curator_sha
                or receipt.private_input_authority_sha256 != reader_authority_sha256
                or receipt.object_count != len(manifest["objects"])
                or registration.previous_registration_sha256 is not None
            ):
                raise ValueError("publication linkage")
            for name in (
                "catalog_sha256",
                "catalog_merkle_root",
                "payload_sha256",
                "transport_sha256",
                "wrapping_key_sha256",
            ):
                if (
                    getattr(registration, name) != manifest[name]
                    or getattr(receipt, name) != manifest[name]
                ):
                    raise ValueError("registration linkage")
            trusted_curator.verify(
                base64.b64decode(receipt.curator_signature_b64, validate=True),
                private_v2_publication_signing_message(
                    manifest=manifest,
                    source_sha=receipt.source_sha,
                    probe_receipt_payload_sha256=receipt.probe_receipt_payload_sha256,
                    private_input_authority_sha256=reader_authority_sha256,
                    curator_signing_key_sha256=curator_sha,
                ),
            )
            if (
                payload.get("schema") != "dittobench-coding-private-payload-v2"
                or payload.get("coding_contract_version") != 2
                or payload.get("weight_eligible") is not False
                or payload.get("task_version_count") != 250
                or payload.get("catalog_sha256") != manifest["catalog_sha256"]
                or payload.get("catalog_merkle_root") != manifest["catalog_merkle_root"]
                or payload.get("payload_sha256") != manifest["payload_sha256"]
                or _digest(
                    {
                        key: value
                        for key, value in payload.items()
                        if key != "payload_sha256"
                    }
                )
                != manifest["payload_sha256"]
                or not isinstance(payload.get("task_assets"), list)
                or len(payload["task_assets"]) != 250
            ):
                raise ValueError("payload linkage")
            self._objects: dict[str, tuple[int, dict[str, Any]]] = {}
            for index, item in enumerate(manifest["objects"]):
                observed = receipt.objects[index]
                remote_key = private_v2_remote_object_key(
                    transport_sha256=manifest["transport_sha256"], object_index=index
                )
                if (
                    observed.object_index != index
                    or observed.ciphertext_sha256 != item["ciphertext_sha256"]
                    or observed.ciphertext_size_bytes != item["ciphertext_size_bytes"]
                    or observed.remote_object_key_sha256
                    != hashlib.sha256(remote_key.encode()).hexdigest()
                    or item["ciphertext_size_bytes"]
                    > PRIVATE_V2_PUBLICATION_MAX_CIPHERTEXT_BYTES
                ):
                    raise ValueError("object linkage")
                self._objects[item["plaintext_sha256"]] = (index, item)
            for index, task in enumerate(payload["task_assets"]):
                if (
                    not isinstance(task, dict)
                    or type(task.get("catalog_index")) is not int
                    or task["catalog_index"] != index
                    or not isinstance(task.get("artifacts"), dict)
                    or set(task["artifacts"]) != _ALL_ROLES
                    or any(
                        digest not in self._objects
                        for digest in task["artifacts"].values()
                    )
                ):
                    raise ValueError("task assets")
        except Exception:
            raise PrivateV2RetrievalError(
                "private v2 retrieval authorities are invalid"
            ) from None
        self._registration = registration
        self._manifest = manifest
        self._payload = payload
        self._audience = audience
        self._grants = grants
        self._reader = reader
        self._unwrapper = unwrapper
        self._clock = clock or (lambda: int(time.time()))

    async def read(self, *, grant_id: UUID, role: str) -> bytes:
        """Return one plaintext object only to the configured trusted Platform role."""
        try:
            async with asyncio.timeout(PRIVATE_V2_RETRIEVAL_TIMEOUT_SECONDS):
                return await self._read(grant_id=grant_id, role=role)
        except Exception:
            raise PrivateV2RetrievalError(
                "private v2 object retrieval failed"
            ) from None

    async def _read(self, *, grant_id: UUID, role: str) -> bytes:
        try:
            grant = await self._grants.active_grant(
                grant_id=grant_id, audience=self._audience
            )
            self._validate_grant(grant, grant_id=grant_id, role=role)
            assert grant is not None
            digest = self._payload["task_assets"][grant.catalog_index]["artifacts"][
                role
            ]
            index, item = self._objects[digest]
            aad = coding_canonical_json_bytes(
                {
                    "schema": "dittobench-coding-private-v2-transport-aad-v1",
                    "payload_sha256": self._manifest["payload_sha256"],
                    "catalog_sha256": self._manifest["catalog_sha256"],
                    "plaintext_sha256": digest,
                    "plaintext_size_bytes": item["plaintext_size_bytes"],
                    "wrapping_key_sha256": self._manifest["wrapping_key_sha256"],
                },
                maximum_bytes=16 << 10,
                label="private v2 transport AAD",
            )
            if hashlib.sha256(aad).hexdigest() != item["aad_sha256"]:
                raise ValueError("AAD")
            key = private_v2_remote_object_key(
                transport_sha256=self._manifest["transport_sha256"], object_index=index
            )
            ciphertext = await self._reader.get_object(
                key=key, max_bytes=item["ciphertext_size_bytes"]
            )
            if (
                len(ciphertext) != item["ciphertext_size_bytes"]
                or hashlib.sha256(ciphertext).hexdigest() != item["ciphertext_sha256"]
            ):
                raise ValueError("ciphertext")
            await self._recheck(grant, role)
            request = PrivateV2UnwrapRequest(
                schema="dittobench-coding-private-v2-unwrap-v1",
                grant_id=str(grant.grant_id),
                evaluation_id=str(grant.evaluation_id),
                attempt_id=str(grant.attempt_id),
                registration_sha256=grant.registration_sha256,
                transport_sha256=self._manifest["transport_sha256"],
                plaintext_sha256=digest,
                ciphertext_sha256=item["ciphertext_sha256"],
                wrapping_key_sha256=self._manifest["wrapping_key_sha256"],
                wrapped_data_key_b64=item["wrapped_data_key_b64"],
                aad_sha256=item["aad_sha256"],
                phase=grant.phase,
                role=role,
                audience=grant.audience,
                expires_at_unix=grant.expires_at_unix,
                frozen_patch_sha256=grant.frozen_patch_sha256,
            )
            unwrapped = await self._unwrapper.unwrap(request)
            if (
                unwrapped.request_sha256 != request.digest()
                or type(unwrapped.data_key) is not bytes
                or len(unwrapped.data_key) != 32
            ):
                raise ValueError("unwrap binding")
            plaintext = AESGCM(unwrapped.data_key).decrypt(
                base64.b64decode(item["nonce_b64"], validate=True), ciphertext, aad
            )
            if (
                len(plaintext) != item["plaintext_size_bytes"]
                or hashlib.sha256(plaintext).hexdigest() != digest
            ):
                raise ValueError("plaintext")
            await self._recheck(grant, role)
            return plaintext
        except Exception:
            raise PrivateV2RetrievalError(
                "private v2 object retrieval failed"
            ) from None

    async def _recheck(self, grant: PrivateV2ObjectGrant, role: str) -> None:
        current = await self._grants.active_grant(
            grant_id=grant.grant_id, audience=self._audience
        )
        if current != grant:
            raise ValueError("grant changed")
        self._validate_grant(current, grant_id=grant.grant_id, role=role)

    def _validate_grant(
        self, grant: PrivateV2ObjectGrant | None, *, grant_id: UUID, role: str
    ) -> None:
        now = self._clock()
        if (
            grant is None
            or type(now) is not int
            or grant.grant_id != grant_id
            or grant.grant_id.int == 0
            or grant.evaluation_id.int == 0
            or grant.attempt_id.int == 0
            or grant.audience != self._audience
            or grant.registration_sha256 != self._registration.registration_sha256
            or type(grant.catalog_index) is not int
            or not 0 <= grant.catalog_index < 250
            or type(grant.expires_at_unix) is not int
            or not now < grant.expires_at_unix <= now + 3600
            or role not in grant.allowed_roles
        ):
            raise ValueError("grant")
        if grant.phase == "authoring":
            if (
                self._audience != "platform-authoring"
                or role not in _AUTHORING_ROLES
                or grant.frozen_patch_sha256 is not None
            ):
                raise ValueError("authoring phase")
        elif grant.phase == "grading":
            digest = grant.frozen_patch_sha256
            if (
                self._audience != "platform-grading"
                or role not in _GRADING_ROLES
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError("grading phase")
        else:
            raise ValueError("phase")


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        coding_canonical_json_bytes(
            value, maximum_bytes=16 << 20, label="private v2 authority"
        )
    ).hexdigest()


def _load_authority(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 8 << 20:
            raise ValueError("authority file")
        body = source.read((8 << 20) + 1)
    value = json.loads(body)
    if (
        not isinstance(value, dict)
        or coding_canonical_json_bytes(
            value, maximum_bytes=8 << 20, label="private v2 authority"
        )
        != body
    ):
        raise ValueError("authority encoding")
    return value
