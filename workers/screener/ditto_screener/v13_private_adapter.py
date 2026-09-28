"""Fail-closed adapters for sealed V13 cases and isolated broker evidence.

This module supplies no production sandbox factory or protected case bank. It
does not expose hidden case bytes, decide policy, or activate replay claims.
The scorer's settled case ledger is currently in-process and unsigned; a
future trusted same-process or authenticated bridge must implement the factory.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from ditto_screening_protocol.v13_private_execute import (
    IsolatedCaseObservation,
    PrivateExecutionUnavailable,
)

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_MANIFEST = 256 * 1024
_MAX_PAYLOAD = 128 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_RESPONSE_NODES = 4096
_MAX_RESPONSE_DEPTH = 64


def _bounded_response_size(response: dict[str, object]) -> int:
    """Count canonical JSON bytes without making a full untrusted JSON copy.

    First bound the parsed tree so ``iterencode`` cannot emit an arbitrarily
    large scalar chunk or recurse through an unbounded/cyclic object. The
    trusted session factory must also cap raw response bytes at ingress before
    parsing; this function is a second fail-closed boundary.
    """
    pending: list[tuple[object, int]] = [(response, 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_RESPONSE_NODES or depth > _MAX_RESPONSE_DEPTH:
            raise PrivateExecutionUnavailable("private case response exceeds bound")
        if type(value) is dict:
            if len(value) > _MAX_RESPONSE_NODES:
                raise PrivateExecutionUnavailable("private case response exceeds bound")
            for key, child in value.items():
                if type(key) is not str or len(key) > _MAX_RESPONSE_BYTES:
                    raise PrivateExecutionUnavailable(
                        "private case response exceeds bound"
                    )
                pending.append((key, depth + 1))
                pending.append((child, depth + 1))
        elif type(value) in (list, tuple):
            sequence = cast(list[object] | tuple[object, ...], value)
            if len(sequence) > _MAX_RESPONSE_NODES:
                raise PrivateExecutionUnavailable("private case response exceeds bound")
            pending.extend((child, depth + 1) for child in sequence)
        elif type(value) is str:
            if len(value) > _MAX_RESPONSE_BYTES:
                raise PrivateExecutionUnavailable("private case response exceeds bound")
        elif type(value) is int:
            if value.bit_length() > _MAX_RESPONSE_BYTES * 4:
                raise PrivateExecutionUnavailable("private case response exceeds bound")
        elif value is not None and type(value) not in (float, bool):
            raise PrivateExecutionUnavailable("private case response unavailable")
    size = 0
    for fragment in json.JSONEncoder(
        sort_keys=True, separators=(",", ":"), allow_nan=False
    ).iterencode(response):
        size += len(fragment.encode())
        if size > _MAX_RESPONSE_BYTES:
            raise PrivateExecutionUnavailable("private case response exceeds bound")
    return size


def _read_digest_object(root: Path, kind: str, digest: str, limit: int) -> bytes:
    if _SHA.fullmatch(digest) is None:
        raise PrivateExecutionUnavailable("sealed object identity invalid")
    root_fd: int | None = None
    kind_fd: int | None = None
    blob_fd: int | None = None
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        kind_fd = os.open(
            kind, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
        )
        blob_fd = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=kind_fd)
        metadata = os.fstat(blob_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise PrivateExecutionUnavailable("sealed object unavailable")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(blob_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != metadata.st_size or hashlib.sha256(raw).hexdigest() != digest:
            raise PrivateExecutionUnavailable("sealed object commitment mismatch")
        return raw
    except PrivateExecutionUnavailable:
        raise
    except OSError:
        raise PrivateExecutionUnavailable("sealed object unavailable") from None
    finally:
        for descriptor in (blob_fd, kind_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


class SealedDigestStore:
    """Digest-addressed read-only view of an externally provisioned private bank.

    ``root`` must be a protected, read-only mount available only to the trusted
    runner. This class never writes case bytes or prints their paths/content.
    Registration and manifest SHA still come from independent Platform state.
    """

    def __init__(self, root: Path | None) -> None:
        self._root = root

    async def read_manifest(self, sha256: str) -> bytes:
        if self._root is None:
            raise PrivateExecutionUnavailable("sealed private bank unavailable")
        return await asyncio.to_thread(
            _read_digest_object, self._root, "manifests", sha256, _MAX_MANIFEST
        )

    async def read_payload(self, sha256: str) -> bytes:
        if self._root is None:
            raise PrivateExecutionUnavailable("sealed private bank unavailable")
        return await asyncio.to_thread(
            _read_digest_object, self._root, "payloads", sha256, _MAX_PAYLOAD
        )


class SettledCaseLedger(BaseModel):
    """Sanitized scorer ledger after case end, revocation, and handler drain."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    session_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal[
        "attributed",
        "cross_case",
        "truncated",
        "unreadable",
        "unattributed",
        "zero_call",
        "no_successful_response",
    ]
    chat_dispatches: int = Field(ge=0)
    successful_responses: int = Field(ge=0)
    attributed_responses: int = Field(ge=0)
    unattributed: int = Field(ge=0)
    unreadable_requests: int = Field(ge=0)
    truncated: bool
    cross_case_starts: int = Field(ge=0)
    cross_case_claims: int = Field(ge=0)


class FreshCaseSession(Protocol):
    """One fresh image/network/state/broker session, never shared across cases."""

    async def seed(self, payload: Mapping[str, object], timeout: float) -> None: ...

    async def run(
        self, payload: Mapping[str, object], timeout: float
    ) -> dict[str, object]: ...

    async def settle(self) -> SettledCaseLedger: ...

    async def close(self) -> None: ...


class TrustedCaseSessionFactory(Protocol):
    """Must verify loaded image ID and return a fresh broker-backed session.

    A production implementation must bind identities from trusted Platform
    state, never miner input, and obtain the settled ledger from the scorer's
    trusted in-process seam or an authenticated signed equivalent. It must cap
    raw response bytes at ingress before parsing. If ``open_case`` raises or
    is cancelled before returning a session, the factory must tear down every
    partially created container, network, state, and broker route itself;
    ``BoundFreshCaseExecutor`` can close only a returned session.
    """

    async def open_case(
        self,
        *,
        role: Literal["target", "known_benign"],
        agent_id: UUID,
        attempt_id: UUID,
        artifact_sha256: str,
        image_sha256: str,
        session_id: str,
        case_id: str,
    ) -> FreshCaseSession: ...


class BoundFreshCaseExecutor:
    """Adapter to V13's per-case protocol, with exact broker-ledger guards.

    Create separate instances for target and known-benign control images using
    independently trusted commitments. Passing ``factory=None`` is safe and
    deliberately unusable: no ordinary screener fake gateway can prove this
    private model-authority ledger.
    """

    def __init__(
        self,
        *,
        role: Literal["target", "known_benign"],
        agent_id: UUID,
        attempt_id: UUID,
        artifact_sha256: str,
        image_sha256: str,
        factory: TrustedCaseSessionFactory | None,
    ) -> None:
        if (
            _SHA.fullmatch(artifact_sha256) is None
            or _SHA.fullmatch(image_sha256) is None
        ):
            raise ValueError("invalid private executor commitment")
        self._role = role
        self._agent_id = agent_id
        self._attempt_id = attempt_id
        self._artifact_sha256 = artifact_sha256
        self._image_sha256 = image_sha256
        self._factory = factory

    async def run_case(
        self,
        *,
        image_sha256: str,
        seed_envelope: Mapping[str, object] | None,
        run_envelope: Mapping[str, object],
        timeout_seconds: float,
    ) -> IsolatedCaseObservation:
        if image_sha256 != self._image_sha256 or self._factory is None:
            raise PrivateExecutionUnavailable("trusted private case route unavailable")
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise PrivateExecutionUnavailable("private case budget invalid")
        # Session/case identities are fresh and never drawn from miner content.
        session_id = str(uuid4())
        case_id = str(uuid4())
        session: FreshCaseSession | None = None
        try:
            # A fresh image/container/network may take longer to prepare than
            # the 30-second case exchange. Keep setup outside the case budget
            # while bounding it independently; never reuse another case state.
            async with asyncio.timeout(300):
                session = await self._factory.open_case(
                    role=self._role,
                    agent_id=self._agent_id,
                    attempt_id=self._attempt_id,
                    artifact_sha256=self._artifact_sha256,
                    image_sha256=self._image_sha256,
                    session_id=session_id,
                    case_id=case_id,
                )
            async with asyncio.timeout(timeout_seconds):
                if seed_envelope is not None:
                    await session.seed(seed_envelope, timeout_seconds)
                response = await session.run(run_envelope, timeout_seconds)
                _bounded_response_size(response)
            # The sidecar must stop accepting work and drain active handlers
            # before its ledger can be used as model-authority evidence.
            async with asyncio.timeout(150):
                ledger = await session.settle()
            expected_session = hashlib.sha256(session_id.encode()).hexdigest()
            expected_case = hmac.new(
                session_id.encode(), case_id.encode(), hashlib.sha256
            ).hexdigest()
            if (
                ledger.session_sha256 != expected_session
                or ledger.case_sha256 != expected_case
                or ledger.agent_id != self._agent_id
                or ledger.attempt_id != self._attempt_id
                or ledger.artifact_sha256 != self._artifact_sha256
                or ledger.image_sha256 != self._image_sha256
            ):
                raise PrivateExecutionUnavailable(
                    "private case ledger identity mismatch"
                )
            verified = (
                ledger.status == "attributed"
                and ledger.chat_dispatches > 0
                and ledger.successful_responses > 0
                and ledger.chat_dispatches == ledger.successful_responses
                and ledger.attributed_responses == ledger.successful_responses
                and ledger.unattributed == 0
                and ledger.unreadable_requests == 0
                and not ledger.truncated
                and ledger.cross_case_starts == 0
                and ledger.cross_case_claims == 0
            )
            return IsolatedCaseObservation(
                response=response, model_authority_verified=verified
            )
        except PrivateExecutionUnavailable:
            raise
        except Exception:
            raise PrivateExecutionUnavailable(
                "private case execution unavailable"
            ) from None
        finally:
            if session is not None:
                try:
                    async with asyncio.timeout(150):
                        await session.close()
                except Exception:
                    raise PrivateExecutionUnavailable(
                        "private case teardown unavailable"
                    ) from None
