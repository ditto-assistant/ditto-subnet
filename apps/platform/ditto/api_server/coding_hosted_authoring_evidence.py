"""Chunked native authoring retention with exact encrypted replay."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid5

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_hosted_control import (
    AuthoringBlob,
    AuthoringIdentity,
    RetentionHeader,
    SourceBinding,
)
from ditto.api_models.coding_inference import _decode_json_document
from ditto.api_server.coding_hippius_custody import RsaOaepHippiusEvidenceKeyWrapper
from ditto.api_server.coding_hippius_evidence import (
    AiobotoHippiusSealedEvidenceTransport,
    HippiusSealedEvidenceConfig,
    HippiusSealedEvidenceNotFound,
    HippiusSealedEvidenceTransport,
)
from ditto.api_server.coding_hippius_probe import load_hippius_probe_receipt
from ditto.api_server.coding_hosted_evidence import _envelope
from ditto.api_server.coding_hosted_evidence_spool import (
    HostedEvidenceError,
    HostedEvidenceSpool,
)
from ditto.db.models import (
    CodingHostedAssignment,
    CodingHostedAuthoringFinalization,
    CodingHostedAuthoringReservation,
)

CHUNK_BYTES = 8 << 20
MAGIC = b"DITTO-AUTHORING-BLOB-V2\0"


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical(value: dict, maximum: int = 16384) -> bytes:
    return coding_canonical_json_bytes(
        value, maximum_bytes=maximum, label="private authoring evidence"
    )


def projection(value) -> dict:
    return value.model_dump(mode="json", by_alias=True)


def aad(identity: dict) -> bytes:
    return canonical(
        {
            k: v
            for k, v in identity.items()
            if k not in {"ciphertext_sha256", "ciphertext_size", "envelope_sha256"}
        }
    )


def check_blob(identity: AuthoringBlob, body: bytes) -> tuple[bytes, bytes, bytes]:
    if (
        len(body) != identity.ciphertext_size
        or sha(body) != identity.ciphertext_sha256
        or not body.startswith(MAGIC)
    ):
        raise HostedEvidenceError("authoring ciphertext differs")
    offset = len(MAGIC)
    nonce = body[offset : offset + 12]
    size = int.from_bytes(body[offset + 12 : offset + 14], "big")
    wrapped = body[offset + 14 : offset + 14 + size]
    ciphertext = body[offset + 14 + size :]
    if (
        not 384 <= size <= 1024
        or len(wrapped) != size
        or len(ciphertext) != identity.plaintext_size + 16
        or _envelope(
            nonce, wrapped, aad(projection(identity)), identity.wrapping_key_sha256
        )
        != identity.envelope_sha256
    ):
        raise HostedEvidenceError("authoring envelope differs")
    return nonce, wrapped, ciphertext


async def owned_source(
    session: AsyncSession, source: SourceBinding, worker_id: UUID
) -> CodingHostedAssignment:
    row = await session.get(CodingHostedAssignment, source.evaluation_id)
    if (
        row is None
        or row.started_at is None
        or row.worker_id != worker_id
        or source.worker_id != worker_id
        or source.attempt_id != row.attempt_id
        or source.assignment_sha256 != row.assignment_sha256
        or source.artifact_sha256 != row.artifact_sha256
        or source.profile_capability_id != f"hosted-{row.attempt_id}"
        or source.deadline_unix != int(row.expires_at.timestamp())
    ):
        raise HostedEvidenceError("authoring worker authority differs")
    return row


def load_authoring_spool(
    spool: HostedEvidenceSpool, object_id: UUID
) -> tuple[dict, bytes] | None:
    value = spool.load(object_id)
    if value is None:
        return None
    raw, body = value
    identity = _decode_json_document(raw, maximum_bytes=16384)
    if not isinstance(identity, dict) or canonical(identity) != raw:
        raise HostedEvidenceError("authoring spool identity is invalid")
    return identity, body


def authoring_evidence_blobs(
    identity: AuthoringIdentity, *, spool: HostedEvidenceSpool, domain: str
):
    chunks = []
    for index in range(identity.chunk_count):
        object_id = uuid5(
            identity.source.attempt_id,
            f"authoring:{identity.manifest.payload_sha256}:{index}",
        )
        item = load_authoring_spool(spool, object_id)
        if item is None:
            raise HostedEvidenceError("authoring chunk missing")
        blob = AuthoringBlob.model_validate_json(canonical(item[0]))
        check_blob(blob, item[1])
        if (
            blob.object_id != object_id
            or blob.ordinal != index
            or blob.source_sha256 != identity.source.digest()
            or blob.payload_sha256 != identity.manifest.payload_sha256
            or blob.storage_domain_sha256 != domain
        ):
            raise HostedEvidenceError("authoring chunk identity differs")
        chunks.append(projection(blob))
        yield blob, item[1]
    if (
        sha(
            canonical(
                {
                    "schema": "dittobench-coding-authoring-chunks-v2",
                    "chunks": chunks,
                },
                1 << 20,
            )
        )
        != identity.chunks_sha256
    ):
        raise HostedEvidenceError("authoring chunk manifest differs")
    top = load_authoring_spool(spool, identity.source.attempt_id)
    if top is None or top[0] != projection(identity):
        raise HostedEvidenceError("authoring manifest missing")
    check_blob(identity.manifest, top[1])
    yield identity.manifest, top[1]


class HostedAuthoringEvidencePublisher:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        worker_id: UUID,
        spool: HostedEvidenceSpool,
        wrapper: RsaOaepHippiusEvidenceKeyWrapper,
        config: HippiusSealedEvidenceConfig,
        probe_receipt_path: Path,
        _test_transport: HippiusSealedEvidenceTransport | None = None,
    ):
        self._sessions, self._worker, self._spool, self._wrapper, self._config = (
            sessions,
            worker_id,
            spool,
            wrapper,
            config,
        )
        self._transport = _test_transport or AiobotoHippiusSealedEvidenceTransport(
            config
        )
        self._probe, self._probe_sha = load_hippius_probe_receipt(probe_receipt_path)
        if self._probe.sealed_evidence_authority_sha256 != config.authority_sha256:
            raise HostedEvidenceError("authoring provider probe differs")
        self._domain = sha(
            canonical(
                {
                    "provider": "hippius",
                    "endpoint": config.endpoint_url,
                    "region": config.region,
                    "bucket": config.bucket,
                }
            )
        )

    def _fresh(self, identity: AuthoringIdentity) -> None:
        checked = datetime.fromisoformat(self._probe.checked_at.replace("Z", "+00:00"))
        if (
            not 0 <= (datetime.now(UTC) - checked).total_seconds() < 86400
            or time.time() >= identity.publication_deadline_unix
            or identity.manifest.storage_domain_sha256 != self._domain
        ):
            raise HostedEvidenceError("authoring publication authority expired")

    def _existing(self, object_id: UUID) -> tuple[dict, bytes] | None:
        return load_authoring_spool(self._spool, object_id)

    async def _prepare(
        self,
        object_id: UUID,
        source_sha: str,
        payload_sha: str,
        ordinal: int,
        plaintext: bytes,
    ) -> tuple[AuthoringBlob, bytes]:
        expected = {
            "schema": "dittobench-coding-authoring-blob-v2",
            "object_id": str(object_id),
            "source_sha256": source_sha,
            "payload_sha256": payload_sha,
            "ordinal": ordinal,
            "plaintext_sha256": sha(plaintext),
            "plaintext_size": len(plaintext),
            "storage_domain_sha256": self._domain,
        }
        stored = self._existing(object_id)
        if stored is not None:
            value, body = stored
            identity = AuthoringBlob.model_validate_json(canonical(value))
            if any(projection(identity).get(k) != v for k, v in expected.items()):
                raise HostedEvidenceError("authoring prepared bytes conflict")
            check_blob(identity, body)
            return identity, body
        expected["wrapping_key_sha256"] = self._wrapper.wrapping_key_sha256
        associated = aad(expected)
        key, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
        encrypted = AESGCM(key).encrypt(nonce, plaintext, associated)
        async with asyncio.timeout(20):
            wrapped = await self._wrapper.wrap_data_key(
                data_key=key, aad_sha256=sha(associated)
            )
        del key
        body = MAGIC + nonce + len(wrapped).to_bytes(2, "big") + wrapped + encrypted
        expected.update(
            ciphertext_sha256=sha(body),
            ciphertext_size=len(body),
            envelope_sha256=_envelope(
                nonce, wrapped, associated, self._wrapper.wrapping_key_sha256
            ),
        )
        identity = AuthoringBlob.model_validate_json(canonical(expected))
        check_blob(identity, body)
        self._spool.store(object_id, canonical(projection(identity)), body)
        return identity, body

    async def prepare(
        self,
        *,
        source: SourceBinding,
        header: RetentionHeader,
        freeze: bytes,
        transcript: AsyncIterator[bytes],
        patch_sha256: str | None,
        patch_size: int,
        inference_evidence_sha256: str | None,
    ) -> AuthoringIdentity:
        # Validate byte commitments independently of the control transport.
        if sha(freeze) != header.freeze_sha256 or len(freeze) != header.freeze_size:
            raise HostedEvidenceError("authoring freeze payload differs")
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            await owned_source(session, source, self._worker)
        payload = {
            "source": projection(source),
            "header": projection(header),
            "patch_sha256": patch_sha256,
            "patch_size": patch_size,
            "inference_evidence_sha256": inference_evidence_sha256,
        }
        payload_sha = sha(canonical(payload))
        async with self._spool.lock:
            existing = self._existing(source.attempt_id)
            if existing is not None:
                identity = AuthoringIdentity.model_validate_json(canonical(existing[0]))
                if any(projection(identity).get(k) != v for k, v in payload.items()):
                    raise HostedEvidenceError("authoring retention conflicts")
                # Still consume and verify the submitted transcript on exact replay.
            chunks: list[dict] = []
            pending = bytearray()

            async def accept(data: bytes) -> None:
                nonlocal pending
                for start in range(0, len(data), CHUNK_BYTES):
                    pending.extend(data[start : start + CHUNK_BYTES])
                    while len(pending) >= CHUNK_BYTES:
                        chunk = bytes(pending[:CHUNK_BYTES])
                        del pending[:CHUNK_BYTES]
                        await seal(chunk)

            async def seal(body: bytes) -> None:
                index = len(chunks)
                if index >= 130:
                    raise HostedEvidenceError("authoring chunk count exceeded")
                object_id = uuid5(source.attempt_id, f"authoring:{payload_sha}:{index}")
                chunk, _ = await self._prepare(
                    object_id, source.digest(), payload_sha, index, body
                )
                chunks.append(projection(chunk))

            await accept(freeze)
            observed = hashlib.sha256()
            size = 0
            async for body in transcript:
                size += len(body)
                if size > header.transcript_size:
                    raise HostedEvidenceError("authoring transcript exceeded bound")
                observed.update(body)
                await accept(body)
            if (
                size != header.transcript_size
                or observed.hexdigest() != header.transcript_sha256
            ):
                raise HostedEvidenceError("authoring transcript differs")
            if pending:
                await seal(bytes(pending))
            chunk_manifest = canonical(
                {"schema": "dittobench-coding-authoring-chunks-v2", "chunks": chunks},
                1 << 20,
            )
            manifest_id = uuid5(source.attempt_id, f"authoring:{payload_sha}:manifest")
            manifest, body = await self._prepare(
                manifest_id, source.digest(), payload_sha, -1, chunk_manifest
            )
            identity = AuthoringIdentity.model_validate_json(
                canonical(
                    dict(
                        payload,
                        schema="dittobench-coding-authoring-evidence-v2",
                        chunk_count=len(chunks),
                        chunks_sha256=sha(chunk_manifest),
                        manifest=projection(manifest),
                        publication_deadline_unix=source.deadline_unix + 86400,
                        weight_eligible=False,
                    )
                )
            )
            if existing is None:
                self._spool.store(
                    source.attempt_id, canonical(projection(identity)), body
                )
            elif existing != (projection(identity), body):
                raise HostedEvidenceError("authoring manifest changed")
            return identity

    def _blobs(self, identity: AuthoringIdentity):
        return authoring_evidence_blobs(
            identity, spool=self._spool, domain=self._domain
        )

    async def resume(self, source: SourceBinding) -> str:
        try:
            top = self._existing(source.attempt_id)
            if top is None:
                raise HostedEvidenceError("authoring evidence not prepared")
            identity = AuthoringIdentity.model_validate_json(canonical(top[0]))
            if identity.source != source:
                raise HostedEvidenceError("authoring source differs")
            async with (
                asyncio.timeout(20),
                self._sessions() as session,
                session.begin(),
            ):
                await owned_source(session, source, self._worker)
                final = await session.get(
                    CodingHostedAuthoringFinalization, source.evaluation_id
                )
                if final is not None:
                    retained = await self.require_retained(
                        session, source, identity.digest()
                    )
                    if retained != identity:
                        raise HostedEvidenceError("authoring finalization differs")
                    return identity.digest()  # Historical acknowledgement, no new I/O.
            self._fresh(identity)
            # Validate every local commitment before any object-store request.
            for _blob, _body in self._blobs(identity):
                pass
            async with (
                asyncio.timeout(20),
                self._sessions() as session,
                session.begin(),
            ):
                await owned_source(session, source, self._worker)
                await session.get(
                    CodingHostedAssignment, source.evaluation_id, with_for_update=True
                )
                row = await session.get(
                    CodingHostedAuthoringReservation, source.evaluation_id
                )
                if row is None:
                    session.add(
                        CodingHostedAuthoringReservation(
                            evaluation_id=source.evaluation_id,
                            identity_sha256=identity.digest(),
                            identity=projection(identity),
                        )
                    )
                elif (
                    row.identity_sha256 != identity.digest()
                    or row.identity != projection(identity)
                ):
                    raise HostedEvidenceError("authoring reservation conflicts")
            for blob, body in self._blobs(identity):
                self._fresh(identity)
                key = (
                    f"coding-hosted-authoring/v2/{blob.object_id.hex}/"
                    f"{blob.ciphertext_sha256}.bin"
                )
                try:
                    async with asyncio.timeout(self._config.timeout_seconds):
                        existing = await self._transport.get_object(
                            key=key, max_bytes=len(body)
                        )
                except HippiusSealedEvidenceNotFound:
                    self._fresh(identity)
                    async with asyncio.timeout(self._config.timeout_seconds):
                        await self._transport.put_object(
                            key=key, body=body, metadata={}
                        )
                else:
                    check_blob(blob, existing)
                self._fresh(identity)
                async with asyncio.timeout(self._config.timeout_seconds):
                    verified = await self._transport.get_object(
                        key=key, max_bytes=len(body)
                    )
                check_blob(blob, verified)
            self._fresh(identity)
            async with (
                asyncio.timeout(20),
                self._sessions() as session,
                session.begin(),
            ):
                await owned_source(session, source, self._worker)
                await session.get(
                    CodingHostedAssignment, source.evaluation_id, with_for_update=True
                )
                if (
                    await session.get(
                        CodingHostedAuthoringFinalization, source.evaluation_id
                    )
                    is None
                ):
                    session.add(
                        CodingHostedAuthoringFinalization(
                            evaluation_id=source.evaluation_id,
                            probe_sha256=self._probe_sha,
                        )
                    )
            return identity.digest()
        except Exception:
            raise HostedEvidenceError(
                "authoring evidence publication did not complete"
            ) from None

    async def require_retained(
        self, session: AsyncSession, source: SourceBinding, digest: str
    ) -> AuthoringIdentity:
        await owned_source(session, source, self._worker)
        row = await session.get(CodingHostedAuthoringReservation, source.evaluation_id)
        finalized = await session.get(
            CodingHostedAuthoringFinalization, source.evaluation_id
        )
        if row is None or finalized is None or row.identity_sha256 != digest:
            raise HostedEvidenceError("authoring evidence is incomplete")
        identity = AuthoringIdentity.model_validate_json(canonical(row.identity))
        if identity.source != source or identity.digest() != digest:
            raise HostedEvidenceError("authoring retained identity differs")
        return identity

    def __repr__(self) -> str:
        return "HostedAuthoringEvidencePublisher(private=True)"
