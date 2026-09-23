"""Synthetic trust-boundary tests; no hidden cases or production keys."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from ditto_screener.v13_private_adapter import (
    BoundFreshCaseExecutor,
    SealedDigestStore,
    SettledCaseLedger,
)
from ditto_screening_protocol.v13_private_execute import PrivateExecutionUnavailable

ARTIFACT = "a" * 64
IMAGE = "b" * 64


@pytest.mark.asyncio
async def test_sealed_store_accepts_only_digest_pinned_regular_files(tmp_path: Path):
    manifests = tmp_path / "manifests"
    payloads = tmp_path / "payloads"
    manifests.mkdir()
    payloads.mkdir()
    raw = b'{"synthetic":true}'
    digest = hashlib.sha256(raw).hexdigest()
    (manifests / digest).write_bytes(raw)
    (payloads / digest).write_bytes(raw)
    store = SealedDigestStore(tmp_path)
    assert await store.read_manifest(digest) == raw
    assert await store.read_payload(digest) == raw

    (manifests / digest).write_bytes(b"changed")
    with pytest.raises(PrivateExecutionUnavailable):
        await store.read_manifest(digest)
    (manifests / digest).unlink()
    (manifests / digest).symlink_to(payloads / digest)
    with pytest.raises(PrivateExecutionUnavailable):
        await store.read_manifest(digest)
    with pytest.raises(PrivateExecutionUnavailable):
        await store.read_payload("../" + digest)
    with pytest.raises(PrivateExecutionUnavailable):
        await SealedDigestStore(None).read_payload(digest)


class SyntheticSession:
    def __init__(
        self,
        *,
        agent_id: UUID,
        attempt_id: UUID,
        session_id: str,
        case_id: str,
        status: str = "attributed",
        wrong_image: bool = False,
        large_response: bool = False,
        nested_response: bool = False,
    ) -> None:
        self.agent_id = agent_id
        self.attempt_id = attempt_id
        self.session_id = session_id
        self.case_id = case_id
        self.status = status
        self.wrong_image = wrong_image
        self.large_response = large_response
        self.nested_response = nested_response
        self.seeded = False
        self.closed = False
        self.settled = False

    async def seed(self, payload, timeout):
        assert payload == {"memory": "synthetic"}
        assert timeout > 0
        self.seeded = True

    async def run(self, payload, timeout):
        assert payload == {"prompt": "synthetic"}
        assert timeout > 0
        if self.nested_response:
            return {
                "answer": "synthetic",
                "nested": [{"value": "x" * 1024} for _ in range(100)],
            }
        return {"answer": "x" * (64 * 1024) if self.large_response else "synthetic"}

    async def settle(self):
        self.settled = True
        return SettledCaseLedger(
            session_sha256=hashlib.sha256(self.session_id.encode()).hexdigest(),
            case_sha256=hmac.new(
                self.session_id.encode(), self.case_id.encode(), hashlib.sha256
            ).hexdigest(),
            agent_id=self.agent_id,
            attempt_id=self.attempt_id,
            artifact_sha256=ARTIFACT,
            image_sha256="c" * 64 if self.wrong_image else IMAGE,
            status=self.status,
            chat_dispatches=1,
            successful_responses=1,
            attributed_responses=1,
            unattributed=0,
            unreadable_requests=0,
            truncated=False,
            cross_case_starts=0,
            cross_case_claims=0,
        )

    async def close(self):
        self.closed = True


class SyntheticFactory:
    def __init__(
        self,
        *,
        status="attributed",
        wrong_image=False,
        large_response=False,
        nested_response=False,
    ):
        self.status = status
        self.wrong_image = wrong_image
        self.large_response = large_response
        self.nested_response = nested_response
        self.sessions: list[SyntheticSession] = []

    async def open_case(self, **kwargs):
        assert kwargs["role"] == "target"
        assert kwargs["artifact_sha256"] == ARTIFACT
        assert kwargs["image_sha256"] == IMAGE
        session = SyntheticSession(
            agent_id=kwargs["agent_id"],
            attempt_id=kwargs["attempt_id"],
            session_id=kwargs["session_id"],
            case_id=kwargs["case_id"],
            status=self.status,
            wrong_image=self.wrong_image,
            large_response=self.large_response,
            nested_response=self.nested_response,
        )
        self.sessions.append(session)
        return session


@pytest.mark.asyncio
async def test_executor_requires_fresh_exact_settled_broker_ledger():
    agent_id, attempt_id = uuid4(), uuid4()
    factory = SyntheticFactory()
    executor = BoundFreshCaseExecutor(
        role="target",
        agent_id=agent_id,
        attempt_id=attempt_id,
        artifact_sha256=ARTIFACT,
        image_sha256=IMAGE,
        factory=factory,
    )
    for _ in range(2):
        outcome = await executor.run_case(
            image_sha256=IMAGE,
            seed_envelope={"memory": "synthetic"},
            run_envelope={"prompt": "synthetic"},
            timeout_seconds=10,
        )
        assert outcome.model_authority_verified is True
    assert len({session.session_id for session in factory.sessions}) == 2
    assert all(session.seeded and session.closed for session in factory.sessions)

    factory.status = "zero_call"
    outcome = await executor.run_case(
        image_sha256=IMAGE,
        seed_envelope=None,
        run_envelope={"prompt": "synthetic"},
        timeout_seconds=10,
    )
    assert outcome.model_authority_verified is False
    assert factory.sessions[-1].closed

    with pytest.raises(PrivateExecutionUnavailable):
        await executor.run_case(
            image_sha256="c" * 64,
            seed_envelope=None,
            run_envelope={},
            timeout_seconds=10,
        )
    assert len(factory.sessions) == 3

    factory.wrong_image = True
    with pytest.raises(PrivateExecutionUnavailable):
        await executor.run_case(
            image_sha256=IMAGE,
            seed_envelope=None,
            run_envelope={"prompt": "synthetic"},
            timeout_seconds=10,
        )
    assert factory.sessions[-1].closed

    factory.wrong_image = False
    factory.large_response = True
    with pytest.raises(PrivateExecutionUnavailable):
        await executor.run_case(
            image_sha256=IMAGE,
            seed_envelope=None,
            run_envelope={"prompt": "synthetic"},
            timeout_seconds=10,
        )
    assert factory.sessions[-1].closed


@pytest.mark.asyncio
async def test_oversized_nested_response_is_bounded_before_ledger_settle():
    factory = SyntheticFactory(nested_response=True)
    executor = BoundFreshCaseExecutor(
        role="target",
        agent_id=uuid4(),
        attempt_id=uuid4(),
        artifact_sha256=ARTIFACT,
        image_sha256=IMAGE,
        factory=factory,
    )
    with pytest.raises(PrivateExecutionUnavailable, match="response exceeds bound"):
        await executor.run_case(
            image_sha256=IMAGE,
            seed_envelope=None,
            run_envelope={"prompt": "synthetic"},
            timeout_seconds=10,
        )
    assert factory.sessions[-1].closed
    assert not factory.sessions[-1].settled


@pytest.mark.asyncio
async def test_executor_fails_closed_without_trusted_factory():
    executor = BoundFreshCaseExecutor(
        role="target",
        agent_id=uuid4(),
        attempt_id=uuid4(),
        artifact_sha256=ARTIFACT,
        image_sha256=IMAGE,
        factory=None,
    )
    with pytest.raises(PrivateExecutionUnavailable):
        await executor.run_case(
            image_sha256=IMAGE,
            seed_envelope=None,
            run_envelope={},
            timeout_seconds=10,
        )
