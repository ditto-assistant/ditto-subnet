"""Reservation-bound ciphertext publication recovery; no execution or key access."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_hosted_control import AuthoringIdentity
from ditto.api_models.coding_hosted_evidence import HostedEvidenceIdentity
from ditto.api_models.coding_hosted_grading import HostedTerminalIdentity
from ditto.api_server.coding_hippius_evidence import (
    AiobotoHippiusSealedEvidenceTransport,
    HippiusSealedEvidenceConfig,
    HippiusSealedEvidenceNotFound,
    HippiusSealedEvidenceTransport,
)
from ditto.api_server.coding_hippius_probe import load_hippius_probe_receipt
from ditto.api_server.coding_hosted_authoring_evidence import (
    authoring_evidence_blobs,
    canonical,
    check_blob,
    load_authoring_spool,
    owned_source,
    projection,
    sha,
)
from ditto.api_server.coding_hosted_evidence import (
    checked_blob,
    inference_evidence_source,
)
from ditto.api_server.coding_hosted_evidence_spool import (
    HostedEvidenceError,
    HostedEvidenceSpool,
)
from ditto.db.models import (
    CodingHostedAssignment,
    CodingHostedAuthoringFinalization,
    CodingHostedAuthoringReservation,
    CodingHostedEvidenceFinalization,
    CodingHostedEvidenceReservation,
    CodingHostedGradingClaim,
    CodingHostedTerminalFinalization,
    CodingHostedTerminalReservation,
)
from ditto.db.queries.coding_hosted_private import close_hosted_private_task


@dataclass(frozen=True, repr=False)
class RecoveryTarget:
    phase: Literal["inference", "authoring", "terminal"]
    worker_id: UUID
    evaluation_id: UUID
    attempt_id: UUID
    identity_sha256: str
    request_id: UUID | None = None

    def check(self) -> None:
        if (
            self.phase not in {"inference", "authoring", "terminal"}
            or any(
                not isinstance(v, UUID) or v.int == 0
                for v in (self.worker_id, self.evaluation_id, self.attempt_id)
            )
            or not isinstance(self.identity_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.identity_sha256) is None
            or (
                self.phase == "inference"
                and (not isinstance(self.request_id, UUID) or not self.request_id.int)
            )
            or (self.phase != "inference" and self.request_id is not None)
        ):
            raise HostedEvidenceError("evidence recovery target invalid")


Identity = HostedEvidenceIdentity | AuthoringIdentity | HostedTerminalIdentity


@dataclass(frozen=True, repr=False)
class RecoveryRecord:
    identity: Identity
    finalized: bool

    @property
    def domain(self) -> str:
        if isinstance(self.identity, HostedEvidenceIdentity):
            return self.identity.storage_domain_sha256
        if isinstance(self.identity, AuthoringIdentity):
            return self.identity.manifest.storage_domain_sha256
        return self.identity.blob.storage_domain_sha256


class HostedEvidenceRecovery:
    """Reopen only a read-only, exclusively locked spool and one exact DB target."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        spool: HostedEvidenceSpool,
        config: HippiusSealedEvidenceConfig | None = None,
        probe_receipt: Path | None = None,
        _test_transport: HippiusSealedEvidenceTransport | None = None,
    ):
        if not spool.read_only:
            raise HostedEvidenceError("recovery requires a read-only spool")
        self._sessions, self._spool = sessions, spool
        self._config, self._probe_path, self._test_transport = (
            config,
            probe_receipt,
            _test_transport,
        )

    async def _record(
        self, session: AsyncSession, target: RecoveryTarget, *, lock: bool = False
    ) -> RecoveryRecord:
        target.check()
        row: (
            CodingHostedEvidenceReservation
            | CodingHostedAuthoringReservation
            | CodingHostedTerminalReservation
            | None
        )
        final: (
            CodingHostedEvidenceFinalization
            | CodingHostedAuthoringFinalization
            | CodingHostedTerminalFinalization
            | None
        )
        identity: Identity
        if target.phase == "inference":
            assert target.request_id is not None
            source = await inference_evidence_source(
                session,
                target.request_id,
                target.worker_id,
                lock=lock,
                allow_expired=True,
            )
            row = await session.get(CodingHostedEvidenceReservation, target.request_id)
            if row is None:
                raise HostedEvidenceError("evidence recovery requires a reservation")
            identity = HostedEvidenceIdentity.model_validate_json(
                canonical(row.identity)
            )
            if any(projection(identity).get(k) != v for k, v in source.fields.items()):
                raise HostedEvidenceError("inference recovery source differs")
            if (
                identity.evaluation_id != target.evaluation_id
                or identity.attempt_id != target.attempt_id
                or identity.reservation_id != row.reservation_id
            ):
                raise HostedEvidenceError("inference recovery target differs")
            final = await session.get(
                CodingHostedEvidenceFinalization, row.reservation_id
            )
        else:
            # Same lock order as the normal publishers: assignment before finalization.
            await session.get(
                CodingHostedAssignment, target.evaluation_id, with_for_update=lock
            )
            if target.phase == "authoring":
                row = await session.get(
                    CodingHostedAuthoringReservation, target.evaluation_id
                )
                if row is None:
                    raise HostedEvidenceError(
                        "evidence recovery requires a reservation"
                    )
                identity = AuthoringIdentity.model_validate_json(
                    canonical(row.identity)
                )
                final = await session.get(
                    CodingHostedAuthoringFinalization, target.evaluation_id
                )
            else:
                row = await session.get(
                    CodingHostedTerminalReservation, target.evaluation_id
                )
                if row is None:
                    raise HostedEvidenceError(
                        "evidence recovery requires a reservation"
                    )
                identity = HostedTerminalIdentity.model_validate_json(
                    canonical(row.identity)
                )
                claim = await session.get(
                    CodingHostedGradingClaim, target.evaluation_id
                )
                if (
                    claim is None
                    or claim.claim_id != identity.claim_id
                    or claim.binding.get("source") != projection(identity.source)
                    or claim.binding.get("authoring_evidence_sha256")
                    != identity.authoring_evidence_sha256
                    or claim.binding.get("grading_profile_sha256")
                    != identity.grading_profile_sha256
                ):
                    raise HostedEvidenceError("terminal recovery claim differs")
                final = await session.get(
                    CodingHostedTerminalFinalization, target.evaluation_id
                )
            if (
                identity.source.evaluation_id != target.evaluation_id
                or identity.source.attempt_id != target.attempt_id
            ):
                raise HostedEvidenceError("evidence recovery target differs")
            await owned_source(session, identity.source, target.worker_id)
            if (
                identity.publication_deadline_unix
                != identity.source.deadline_unix + 86400
            ):
                raise HostedEvidenceError("recovery publication window differs")
        if (
            identity.digest() != row.identity_sha256
            or identity.digest() != target.identity_sha256
            or projection(identity) != row.identity
        ):
            raise HostedEvidenceError("evidence recovery identity differs")
        return RecoveryRecord(identity, final is not None)

    async def _snapshot(
        self, target: RecoveryTarget, *, publication: bool = False
    ) -> RecoveryRecord:
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            record = await self._record(session, target)
            if publication and not record.finalized:
                now = await session.scalar(select(func.clock_timestamp()))
                if (
                    not isinstance(now, datetime)
                    or now.timestamp() >= record.identity.publication_deadline_unix
                ):
                    raise HostedEvidenceError("recovery publication window expired")
            return record

    def _blobs(self, record: RecoveryRecord):
        identity = record.identity
        if isinstance(identity, HostedEvidenceIdentity):
            stored = self._spool.load(identity.request_id)
            if stored is None or stored[0] != canonical(projection(identity)):
                raise HostedEvidenceError("inference recovery capture missing")
            checked_blob(identity, stored[1])
            yield (
                f"coding-hosted-evidence/v2/{identity.reservation_id.hex}/{identity.ciphertext_sha256}.bin",
                stored[1],
                identity,
            )
        elif isinstance(identity, AuthoringIdentity):
            # The commit marker must match before any chunks can be published.
            top = load_authoring_spool(self._spool, identity.source.attempt_id)
            if top is None or top[0] != projection(identity):
                raise HostedEvidenceError("authoring recovery capture missing")
            for blob, body in authoring_evidence_blobs(
                identity, spool=self._spool, domain=record.domain
            ):
                yield (
                    f"coding-hosted-authoring/v2/{blob.object_id.hex}/{blob.ciphertext_sha256}.bin",
                    body,
                    blob,
                )
        else:
            object_id = uuid5(identity.source.attempt_id, "hosted-terminal-record-v2")
            stored = self._spool.load(object_id)
            if stored is None or stored[0] != canonical(projection(identity)):
                raise HostedEvidenceError("terminal recovery capture missing")
            check_blob(identity.blob, stored[1])
            blob = identity.blob
            if (
                blob.object_id
                != uuid5(identity.source.attempt_id, "hosted-terminal-blob-v2")
                or blob.source_sha256 != identity.source.digest()
                or blob.payload_sha256 != identity.result_sha256
                or blob.ordinal != -1
            ):
                raise HostedEvidenceError("terminal recovery blob differs")
            yield (
                f"coding-hosted-terminal/v2/{blob.object_id.hex}/{blob.ciphertext_sha256}.bin",
                stored[1],
                blob,
            )

    def _inspect(self, record: RecoveryRecord) -> tuple[int, int]:
        count, size = 0, 0
        maximum, objects = self._spool.bounds
        for _key, body, _identity in self._blobs(record):
            count, size = count + 1, size + len(body)
            if count > objects or size > maximum:
                raise HostedEvidenceError("recovery selection exceeds capacity")
        return count, size

    @staticmethod
    def _receipt(target: RecoveryTarget, state: str, count: int, size: int) -> dict:
        return {
            "schema": "dittobench-coding-evidence-recovery-v2",
            "phase": target.phase,
            "evaluation_id": str(target.evaluation_id),
            "attempt_id": str(target.attempt_id),
            "identity_sha256": target.identity_sha256,
            "state": state,
            "objects": count,
            "ciphertext_bytes": size,
            "reexecuted": False,
            "shadow_only": True,
            "weight_eligible": False,
        }

    async def inspect(self, target: RecoveryTarget) -> dict:
        try:
            async with self._spool.lock:
                record = await self._snapshot(target)
                count, size = self._inspect(record)
                return self._receipt(
                    target,
                    "already_finalized" if record.finalized else "prepared",
                    count,
                    size,
                )
        except Exception:
            raise HostedEvidenceError(
                "native evidence recovery inspection failed"
            ) from None

    async def resume(self, target: RecoveryTarget) -> dict:
        try:
            async with asyncio.timeout(3600), self._spool.lock:
                record = await self._snapshot(target, publication=True)
                count, size = self._inspect(record)
                if record.finalized:
                    return self._receipt(target, "already_finalized", count, size)
                if self._config is None or self._probe_path is None:
                    raise HostedEvidenceError("recovery publication is not configured")
                config = self._config
                probe, probe_sha = load_hippius_probe_receipt(self._probe_path)
                domain = sha(
                    canonical(
                        {
                            "provider": "hippius",
                            "endpoint": config.endpoint_url,
                            "region": config.region,
                            "bucket": config.bucket,
                        }
                    )
                )

                def fresh(now: datetime | None = None):
                    now = now or datetime.now(UTC)
                    checked = datetime.fromisoformat(
                        probe.checked_at.replace("Z", "+00:00")
                    )
                    if (
                        probe.sealed_evidence_authority_sha256
                        != config.authority_sha256
                        or domain != record.domain
                        or not 0 <= (now - checked).total_seconds() < 86400
                        or now.timestamp() >= record.identity.publication_deadline_unix
                    ):
                        raise HostedEvidenceError(
                            "recovery publication authority expired or differs"
                        )

                fresh()
                transport = (
                    self._test_transport
                    or AiobotoHippiusSealedEvidenceTransport(config)
                )
                for key, body, identity in self._blobs(record):
                    fresh()
                    checker = (
                        checked_blob
                        if isinstance(identity, HostedEvidenceIdentity)
                        else check_blob
                    )
                    try:
                        async with asyncio.timeout(config.timeout_seconds):
                            found = await transport.get_object(
                                key=key, max_bytes=len(body)
                            )
                    except HippiusSealedEvidenceNotFound:
                        fresh()
                        async with asyncio.timeout(config.timeout_seconds):
                            await transport.put_object(key=key, body=body, metadata={})
                    else:
                        checker(identity, found)
                    fresh()
                    async with asyncio.timeout(config.timeout_seconds):
                        found = await transport.get_object(key=key, max_bytes=len(body))
                    checker(identity, found)
                fresh()
                async with (
                    asyncio.timeout(20),
                    self._sessions() as session,
                    session.begin(),
                ):
                    current = await self._record(session, target, lock=True)
                    database_now = await session.scalar(select(func.clock_timestamp()))
                    if not isinstance(database_now, datetime):
                        raise HostedEvidenceError("recovery database clock unavailable")
                    fresh(database_now)
                    if current.identity != record.identity:
                        raise HostedEvidenceError("recovery reservation changed")
                    if not current.finalized:
                        if isinstance(current.identity, HostedEvidenceIdentity):
                            session.add(
                                CodingHostedEvidenceFinalization(
                                    reservation_id=current.identity.reservation_id,
                                    probe_sha256=probe_sha,
                                )
                            )
                        elif isinstance(current.identity, AuthoringIdentity):
                            session.add(
                                CodingHostedAuthoringFinalization(
                                    evaluation_id=target.evaluation_id,
                                    probe_sha256=probe_sha,
                                )
                            )
                        else:
                            session.add(
                                CodingHostedTerminalFinalization(
                                    evaluation_id=target.evaluation_id,
                                    probe_sha256=probe_sha,
                                )
                            )
                            await close_hosted_private_task(
                                session,
                                evaluation_id=target.evaluation_id,
                                attempt_id=target.attempt_id,
                                worker_id=target.worker_id,
                                reason="completed"
                                if current.identity.outcome == "completed"
                                else "failed",
                            )
                return self._receipt(target, "published", count, size)
        except Exception:
            raise HostedEvidenceError(
                "native evidence recovery publication failed"
            ) from None

    async def verify_readback(self, target: RecoveryTarget) -> dict:
        """Fresh exact-key reads of finalized ciphertext; never upload or finalize."""
        try:
            async with asyncio.timeout(3600), self._spool.lock:
                record = await self._snapshot(target)
                if (
                    not record.finalized
                    or self._config is None
                    or self._probe_path is None
                ):
                    raise HostedEvidenceError("finalized readback is unavailable")
                count, size = self._inspect(record)
                config = self._config
                probe, probe_sha = load_hippius_probe_receipt(self._probe_path)
                domain = sha(
                    canonical(
                        {
                            "provider": "hippius",
                            "endpoint": config.endpoint_url,
                            "region": config.region,
                            "bucket": config.bucket,
                        }
                    )
                )

                def fresh():
                    checked = datetime.fromisoformat(
                        probe.checked_at.replace("Z", "+00:00")
                    )
                    if (
                        probe.sealed_evidence_authority_sha256
                        != config.authority_sha256
                        or domain != record.domain
                        or not 0
                        <= (datetime.now(UTC) - checked).total_seconds()
                        < 86400
                    ):
                        raise HostedEvidenceError("readback storage authority differs")

                fresh()
                transport = (
                    self._test_transport
                    or AiobotoHippiusSealedEvidenceTransport(config)
                )
                for key, body, identity in self._blobs(record):
                    fresh()
                    async with asyncio.timeout(config.timeout_seconds):
                        found = await transport.get_object(key=key, max_bytes=len(body))
                    checker = (
                        checked_blob
                        if isinstance(identity, HostedEvidenceIdentity)
                        else check_blob
                    )
                    checker(identity, found)
                fresh()
                if await self._snapshot(target) != record:
                    raise HostedEvidenceError("readback identity changed")
                return {
                    **self._receipt(target, "verified", count, size),
                    "schema": "dittobench-coding-evidence-readback-v2",
                    "probe_sha256": probe_sha,
                    "storage_domain_sha256": domain,
                    "checked_at": datetime.now(UTC).isoformat(),
                    "uploaded": False,
                }
        except Exception:
            raise HostedEvidenceError("native evidence readback failed") from None
