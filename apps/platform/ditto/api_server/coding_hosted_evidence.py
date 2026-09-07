"""Native inference evidence retention, exact replay, and Hippius readback."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_hosted_evidence import (
    MAX_PLAINTEXT,
    HostedEvidenceIdentity,
)
from ditto.api_models.coding_hosted_inference import (
    HostedInferencePolicy,
    HostedInferenceSettlement,
)
from ditto.api_models.coding_inference import (
    coding_inference_canonical_json_bytes,
    parse_coding_inference_json,
)
from ditto.api_server.coding_hippius_custody import RsaOaepHippiusEvidenceKeyWrapper
from ditto.api_server.coding_hippius_evidence import (
    AiobotoHippiusSealedEvidenceTransport,
    HippiusSealedEvidenceConfig,
    HippiusSealedEvidenceNotFound,
    HippiusSealedEvidenceTransport,
)
from ditto.api_server.coding_hippius_probe import load_hippius_probe_receipt
from ditto.api_server.coding_hosted_budget import ProfiledBudgetEstimator
from ditto.api_server.coding_hosted_evidence_spool import (
    HostedEvidenceError,
    HostedEvidenceSpool,
)
from ditto.api_server.coding_hosted_inference import DispatchReservation
from ditto.api_server.coding_hosted_provider import ProviderResult, verify_response
from ditto.db.models import (
    CodingHostedEvidenceFinalization,
    CodingHostedEvidenceReservation,
    CodingHostedInferenceGrant,
    CodingHostedInferenceRequest,
)
from ditto.db.queries.coding_hosted_admission import _now

MAGIC = b"DITTO-EVIDENCE-V2\0"


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _canonical(value: dict, limit: int = 16384) -> bytes:
    return coding_inference_canonical_json_bytes(value, maximum_bytes=limit)


def _aad(identity: dict) -> bytes:
    value = {
        key: item
        for key, item in identity.items()
        if key not in {"ciphertext_sha256", "ciphertext_size", "envelope_sha256"}
    }
    value["schema"] = "dittobench-coding-hosted-evidence-aad-v2"
    return _canonical(value)


def _envelope(
    nonce: bytes, wrapped: bytes, aad: bytes, wrapping_key_sha256: str
) -> str:
    return _sha(
        _canonical(
            {
                "schema": "dittobench-coding-hosted-evidence-envelope-v2",
                "algorithm": "aes256-gcm/rsa-oaep-sha256",
                "nonce_b64": base64.b64encode(nonce).decode(),
                "wrapped_key_b64": base64.b64encode(wrapped).decode(),
                "aad_sha256": _sha(aad),
                "wrapping_key_sha256": wrapping_key_sha256,
            }
        )
    )


def checked_blob(
    identity: HostedEvidenceIdentity, body: bytes
) -> tuple[bytes, bytes, bytes]:
    if (
        len(body) != identity.ciphertext_size
        or _sha(body) != identity.ciphertext_sha256
        or not body.startswith(MAGIC)
    ):
        raise HostedEvidenceError("native evidence bytes differ")
    offset = len(MAGIC)
    nonce = body[offset : offset + 12]
    size = int.from_bytes(body[offset + 12 : offset + 14], "big")
    wrapped = body[offset + 14 : offset + 14 + size]
    ciphertext = body[offset + 14 + size :]
    if (
        not 384 <= size <= 1024
        or len(wrapped) != size
        or len(ciphertext) != identity.plaintext_size + 16
    ):
        raise HostedEvidenceError("native evidence frame is invalid")
    aad = _aad(identity.model_dump(mode="json", by_alias=True))
    if (
        _envelope(nonce, wrapped, aad, identity.wrapping_key_sha256)
        != identity.envelope_sha256
    ):
        raise HostedEvidenceError("native evidence envelope differs")
    return nonce, wrapped, ciphertext


@dataclass(frozen=True, repr=False)
class _Source:
    fields: dict
    policy: HostedInferencePolicy
    settlement: HostedInferenceSettlement
    reservation: DispatchReservation
    started_at: float


@dataclass(frozen=True)
class HostedEvidenceReceipt:
    reservation_id: UUID
    identity_sha256: str
    ciphertext_sha256: str
    ciphertext_size: int
    verified_at: datetime
    weight_eligible: bool = False


async def inference_evidence_source(
    session: AsyncSession,
    request_id: UUID,
    worker_id: UUID,
    *,
    lock: bool = False,
    allow_expired: bool = False,
) -> _Source:
    row = await session.get(
        CodingHostedInferenceRequest, request_id, with_for_update=lock
    )
    if (
        row is None
        or row.state != "settled"
        or row.finalized_at is None
        or row.settlement is None
    ):
        raise HostedEvidenceError("native inference settlement is unavailable")
    grant = await session.get(CodingHostedInferenceGrant, row.grant_id)
    if grant is None or grant.worker_id != worker_id:
        raise HostedEvidenceError("native evidence worker does not own request")
    policy = HostedInferencePolicy.model_validate(grant.policy)
    settlement = HostedInferenceSettlement.model_validate(row.settlement)
    if (
        policy.runtime_profile_sha256 is None
        or policy.digest() != grant.policy_sha256
        or settlement.digest() != row.settlement_sha256
    ):
        raise HostedEvidenceError("native evidence source identity differs")
    deadline = int(row.finalized_at.timestamp()) + 86400
    if not allow_expired and (await _now(session)).timestamp() >= deadline:
        raise HostedEvidenceError("native evidence publication window expired")
    fields = {
        "request_id": str(request_id),
        "grant_id": str(grant.grant_id),
        "evaluation_id": str(grant.evaluation_id),
        "attempt_id": str(grant.attempt_id),
        "worker_id": str(worker_id),
        "assignment_sha256": grant.assignment_sha256,
        "policy_sha256": grant.policy_sha256,
        "settlement_sha256": row.settlement_sha256,
        "runtime_profile_sha256": policy.runtime_profile_sha256,
        "publication_deadline_unix": deadline,
    }
    reservation = DispatchReservation(
        grant.grant_id,
        request_id,
        row.sequence,
        row.locked_request_sha256,
        False,
        int(grant.expires_at.timestamp()),
        grant.evaluation_id,
        grant.attempt_id,
        grant.policy_sha256,
    )
    return _Source(fields, policy, settlement, reservation, row.created_at.timestamp())


class HostedInferenceEvidencePublisher:
    """Trusted Platform owner only. No public route, activation or credentials API.

    __call__ satisfies HostedRelayBridge.retain_evidence. resume only publishes
    already-durable bytes; it never calls the model, re-encrypts, or unwraps keys.
    """

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        worker_id: UUID,
        spool: HostedEvidenceSpool,
        wrapper: RsaOaepHippiusEvidenceKeyWrapper,
        runtime_profile: bytes,
        config: HippiusSealedEvidenceConfig,
        probe_receipt_path: Path,
        _test_transport: HippiusSealedEvidenceTransport | None = None,
    ):
        if not isinstance(worker_id, UUID) or not worker_id.int:
            raise HostedEvidenceError("native evidence worker is invalid")
        self._sessions, self._worker, self._spool = sessions, worker_id, spool
        self._wrapper, self._profile, self._config = (
            wrapper,
            bytes(runtime_profile),
            config,
        )
        self._transport = _test_transport or AiobotoHippiusSealedEvidenceTransport(
            config
        )
        self._probe, self._probe_sha = load_hippius_probe_receipt(probe_receipt_path)
        if self._probe.sealed_evidence_authority_sha256 != config.authority_sha256:
            raise HostedEvidenceError("Hippius evidence authority does not match probe")
        # Excludes access ID so fresh scoped credential rotation can replay the
        # same reserved object, but another bucket/origin can never substitute.
        self._domain = _sha(
            _canonical(
                {
                    "provider": "hippius",
                    "endpoint": config.endpoint_url,
                    "region": config.region,
                    "bucket": config.bucket,
                }
            )
        )

    async def _source(
        self, session: AsyncSession, request_id: UUID, *, lock: bool = False
    ) -> _Source:
        return await inference_evidence_source(
            session, request_id, self._worker, lock=lock
        )

    async def _snapshot(self, request_id: UUID) -> _Source:
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            return await self._source(session, request_id)

    @staticmethod
    def _match(identity: HostedEvidenceIdentity, source: _Source) -> None:
        actual = identity.model_dump(mode="json", by_alias=True)
        if any(actual.get(key) != value for key, value in source.fields.items()):
            raise HostedEvidenceError("native evidence authority drifted")

    def _load(self, request_id: UUID) -> tuple[HostedEvidenceIdentity, bytes] | None:
        stored = self._spool.load(request_id)
        if stored is None:
            return None
        manifest, body = stored
        identity = parse_coding_inference_json(
            HostedEvidenceIdentity, manifest, maximum_bytes=16384
        )
        if (
            identity.request_id != request_id
            or _canonical(identity.model_dump(mode="json", by_alias=True)) != manifest
        ):
            raise HostedEvidenceError("native spool identity differs")
        checked_blob(identity, body)
        return identity, body

    async def __call__(self, result: ProviderResult) -> None:
        try:
            source = await self._snapshot(result.settlement.request_id)
            verified = verify_response(
                result.provider_evidence, source.policy, source.reservation
            )
            if (
                verified.response != result.response
                or verified.settlement != result.settlement
                or result.settlement != source.settlement
            ):
                raise HostedEvidenceError(
                    "native provider evidence differs from settlement"
                )
            profile = ProfiledBudgetEstimator(
                profile_bytes=self._profile,
                policy=source.policy,
                now=lambda: source.started_at,
            )
            profile.verify_usage(source.settlement)
            plaintext = _canonical(
                {
                    "schema": "dittobench-coding-hosted-inference-evidence-payload-v2",
                    "policy": source.policy.model_dump(mode="json", by_alias=True),
                    "runtime_profile_base64": base64.b64encode(
                        profile.canonical_profile()
                    ).decode(),
                    "settlement": source.settlement.model_dump(
                        mode="json", by_alias=True
                    ),
                    "response_base64": base64.b64encode(result.response).decode(),
                    "provider_evidence_base64": base64.b64encode(
                        result.provider_evidence
                    ).decode(),
                },
                MAX_PLAINTEXT,
            )
            async with self._spool.lock:
                existing = self._load(result.settlement.request_id)
                if existing is None:
                    fields = dict(
                        source.fields,
                        schema="dittobench-coding-hosted-inference-evidence-v2",
                        reservation_id=str(uuid4()),
                        storage_domain_sha256=self._domain,
                        wrapping_key_sha256=self._wrapper.wrapping_key_sha256,
                        plaintext_sha256=_sha(plaintext),
                        plaintext_size=len(plaintext),
                        weight_eligible=False,
                    )
                    aad = _aad(fields)
                    data_key, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
                    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, aad)
                    async with asyncio.timeout(20):
                        wrapped = await self._wrapper.wrap_data_key(
                            data_key=data_key, aad_sha256=_sha(aad)
                        )
                    del data_key  # No claim of secure zeroization in Python.
                    if (
                        not isinstance(wrapped, bytes)
                        or not 384 <= len(wrapped) <= 1024
                    ):
                        raise HostedEvidenceError("native evidence wrapping failed")
                    body = (
                        MAGIC
                        + nonce
                        + len(wrapped).to_bytes(2, "big")
                        + wrapped
                        + ciphertext
                    )
                    fields.update(
                        ciphertext_sha256=_sha(body),
                        ciphertext_size=len(body),
                        envelope_sha256=_envelope(
                            nonce, wrapped, aad, self._wrapper.wrapping_key_sha256
                        ),
                    )
                    identity = HostedEvidenceIdentity.model_validate_json(
                        _canonical(fields)
                    )
                    checked_blob(identity, body)
                    self._spool.store(
                        identity.request_id,
                        _canonical(identity.model_dump(mode="json", by_alias=True)),
                        body,
                    )
                else:
                    identity, _ = existing
                    self._match(identity, source)
                    if identity.plaintext_sha256 != _sha(plaintext):
                        raise HostedEvidenceError("different evidence already exists")
            await self.resume(result.settlement.request_id)
        except Exception:
            raise HostedEvidenceError(
                "native evidence retention did not complete"
            ) from None

    def _check_probe(self, identity: HostedEvidenceIdentity) -> None:
        now = datetime.now(UTC)
        checked = datetime.fromisoformat(self._probe.checked_at.replace("Z", "+00:00"))
        if (
            not 0 <= (now - checked).total_seconds() < 86400
            or now.timestamp() >= identity.publication_deadline_unix
            or identity.storage_domain_sha256 != self._domain
        ):
            raise HostedEvidenceError(
                "native evidence provider authority is unavailable"
            )

    async def resume(self, request_id: UUID) -> HostedEvidenceReceipt:
        try:
            stored = self._load(request_id)
            if stored is None:
                raise HostedEvidenceError("native evidence is not durably prepared")
            identity, body = stored
            self._check_probe(identity)
            # This transaction commits exact identity before any provider I/O.
            async with (
                asyncio.timeout(20),
                self._sessions() as session,
                session.begin(),
            ):
                source = await self._source(session, request_id, lock=True)
                self._match(identity, source)
                old = await session.get(CodingHostedEvidenceReservation, request_id)
                if old is None:
                    session.add(
                        CodingHostedEvidenceReservation(
                            request_id=request_id,
                            reservation_id=identity.reservation_id,
                            identity_sha256=identity.digest(),
                            identity=identity.model_dump(mode="json", by_alias=True),
                        )
                    )
                elif (
                    old.identity_sha256 != identity.digest()
                    or old.identity != identity.model_dump(mode="json", by_alias=True)
                ):
                    raise HostedEvidenceError("native evidence reservation conflicts")
            key = (
                f"coding-hosted-evidence/v2/{identity.reservation_id.hex}/"
                f"{identity.ciphertext_sha256}.bin"
            )
            self._check_probe(identity)
            try:
                async with asyncio.timeout(self._config.timeout_seconds):
                    existing = await self._transport.get_object(
                        key=key, max_bytes=len(body)
                    )
            except HippiusSealedEvidenceNotFound:
                self._check_probe(identity)
                async with asyncio.timeout(self._config.timeout_seconds):
                    await self._transport.put_object(key=key, body=body, metadata={})
            else:
                checked_blob(identity, existing)  # Never overwrite conflicting bytes.
            self._check_probe(identity)
            async with asyncio.timeout(self._config.timeout_seconds):
                verified = await self._transport.get_object(
                    key=key, max_bytes=len(body)
                )
            checked_blob(identity, verified)
            self._check_probe(identity)
            async with (
                asyncio.timeout(20),
                self._sessions() as session,
                session.begin(),
            ):
                self._match(
                    identity, await self._source(session, request_id, lock=True)
                )
                row = await session.get(
                    CodingHostedEvidenceFinalization, identity.reservation_id
                )
                if row is None:
                    row = CodingHostedEvidenceFinalization(
                        reservation_id=identity.reservation_id,
                        probe_sha256=self._probe_sha,
                    )
                    session.add(row)
                    await session.flush()
                receipt = HostedEvidenceReceipt(
                    identity.reservation_id,
                    identity.digest(),
                    identity.ciphertext_sha256,
                    identity.ciphertext_size,
                    row.verified_at,
                )
            return receipt
        except Exception:
            raise HostedEvidenceError(
                "native evidence publication did not complete"
            ) from None

    async def require_complete(self, grant_id: UUID) -> str:
        """Private terminal prerequisite, not a grade or physical-drain proof."""
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            grant = await session.get(CodingHostedInferenceGrant, grant_id)
            if (
                grant is None
                or grant.worker_id != self._worker
                or grant.revoked_at is None
            ):
                raise HostedEvidenceError("native evidence grant is not closed")
            rows = (
                await session.scalars(
                    select(CodingHostedInferenceRequest)
                    .where(CodingHostedInferenceRequest.grant_id == grant_id)
                    .order_by(CodingHostedInferenceRequest.sequence)
                )
            ).all()
            digests = []
            for request in rows:
                reservation = await session.get(
                    CodingHostedEvidenceReservation, request.request_id
                )
                if (
                    request.state != "settled"
                    or reservation is None
                    or await session.get(
                        CodingHostedEvidenceFinalization, reservation.reservation_id
                    )
                    is None
                ):
                    raise HostedEvidenceError("native inference evidence is incomplete")
                identity = HostedEvidenceIdentity.model_validate_json(
                    _canonical(reservation.identity)
                )
                if (
                    identity.digest() != reservation.identity_sha256
                    or identity.settlement_sha256 != request.settlement_sha256
                ):
                    raise HostedEvidenceError(
                        "native evidence identity is inconsistent"
                    )
                digests.append(reservation.identity_sha256)
            return _sha(
                _canonical(
                    {
                        "schema": "dittobench-coding-hosted-inference-evidence-set-v2",
                        "grant_id": str(grant_id),
                        "identities": digests,
                    },
                    32768,
                )
            )

    def __repr__(self) -> str:
        return "HostedInferenceEvidencePublisher(private=True)"
