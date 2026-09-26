"""Signed public moderation records on the score audit chain."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TypedDict
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.db.queries.audit import (
    EVENT_SCORE,
    append_audit_entry,
    list_audit_entries,
    verify_audit_chain,
)
from ditto.db.queries.moderation_audit import (
    ACTION_REJECT,
    ACTION_RELEASE,
    ACTION_RESCREEN,
    DuplicateModerationAction,
    ModerationAuditUnavailable,
    configure_moderation_signer,
    preview_moderation_record,
    published_signer_public_keys,
    record_moderation_audit,
    record_moderation_audit_if_enabled,
    redact_moderation_payload,
    reset_moderation_signer,
    verify_moderation_payload,
)

_T0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
_ARTIFACT = "ab" * 32


def _install(
    private: Ed25519PrivateKey | None, previous: tuple[bytes, ...] = ()
) -> None:
    configure_moderation_signer(private, previous=previous)


class _ModerationArgs(TypedDict):
    action_type: str
    agent_id: UUID
    miner_hotkey: str | None
    artifact_sha256: str
    screened_image_sha256: str | None
    previous_status: str
    resulting_status: str
    recorded_at: datetime
    related_action_id: str | None
    action_id: UUID | None


def _kwargs(
    *,
    action_type: str = ACTION_REJECT,
    agent_id: UUID | None = None,
    miner_hotkey: str | None = "5MinerHotkey",
    previous_status: str = "quarantined",
    resulting_status: str = "rejected",
    recorded_at: datetime = _T0,
    related_action_id: str | None = None,
    action_id: UUID | None = None,
) -> _ModerationArgs:
    return {
        "action_type": action_type,
        "agent_id": agent_id or uuid4(),
        "miner_hotkey": miner_hotkey,
        "artifact_sha256": _ARTIFACT,
        "screened_image_sha256": "cd" * 32,
        "previous_status": previous_status,
        "resulting_status": resulting_status,
        "recorded_at": recorded_at,
        "related_action_id": related_action_id,
        "action_id": action_id,
    }


@pytest.fixture(autouse=True)
def _signer() -> Iterator[None]:
    reset_moderation_signer()
    _install(Ed25519PrivateKey.generate())
    yield
    reset_moderation_signer()


class TestModerationAudit:
    async def test_default_rollout_keeps_moderation_operational_without_key(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DITTO_MODERATION_AUDIT_ENABLED", raising=False)
        monkeypatch.delenv("DITTO_MODERATION_AUDIT_SIGNING_KEY", raising=False)
        _install(None)

        async with session.begin():
            assert (
                await record_moderation_audit_if_enabled(session, **_kwargs()) is None
            )
        assert not [
            row
            for row in await list_audit_entries(session)
            if row.event == "moderation"
        ]

    async def test_active_rollout_requires_signed_record(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DITTO_MODERATION_AUDIT_ENABLED", "true")
        monkeypatch.delenv("DITTO_MODERATION_AUDIT_SIGNING_KEY", raising=False)
        _install(None)
        with pytest.raises(ModerationAuditUnavailable):
            async with session.begin():
                await record_moderation_audit_if_enabled(session, **_kwargs())
        assert not [
            row
            for row in await list_audit_entries(session)
            if row.event == "moderation"
        ]

        await session.rollback()
        _install(Ed25519PrivateKey.generate())
        async with session.begin():
            entry = await record_moderation_audit_if_enabled(session, **_kwargs())
        assert entry is not None
        assert verify_moderation_payload(entry.payload)

    async def test_record_is_signed_and_linked_into_the_public_chain(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            await append_audit_entry(
                session,
                agent_id=uuid4(),
                validator_hotkey="5Validator",
                event=EVENT_SCORE,
                payload={"composite": 0.5},
                recorded_at=_T0,
            )
            entry = await record_moderation_audit(session, **_kwargs())
        entries = await list_audit_entries(session)
        assert verify_audit_chain(entries) is True
        assert entries[-1].entry_hash == entry.entry_hash
        assert entries[-1].prev_hash == entries[0].entry_hash
        assert verify_moderation_payload(entry.payload) is True
        assert entry.payload["signer_key_id"]
        assert published_signer_public_keys()

    async def test_duplicate_action_id_is_rejected(self, session: AsyncSession) -> None:
        action_id = uuid4()
        async with session.begin():
            await record_moderation_audit(session, **_kwargs(action_id=action_id))
        with pytest.raises(DuplicateModerationAction):
            async with session.begin():
                await record_moderation_audit(session, **_kwargs(action_id=action_id))
        entries = await list_audit_entries(session)
        assert len([row for row in entries if row.event == "moderation"]) == 1

    async def test_failed_transaction_does_not_publish(
        self, session: AsyncSession
    ) -> None:
        with pytest.raises(RuntimeError, match="later failure"):
            async with session.begin():
                await record_moderation_audit(session, **_kwargs())
                raise RuntimeError("later failure")
        assert await list_audit_entries(session) == []

    async def test_unavailable_signer_rolls_the_transaction_back(
        self, session: AsyncSession
    ) -> None:
        configure_moderation_signer(None)
        with pytest.raises(ModerationAuditUnavailable):
            async with session.begin():
                await append_audit_entry(
                    session,
                    agent_id=uuid4(),
                    validator_hotkey=None,
                    event=EVENT_SCORE,
                    payload={"composite": 0.1},
                    recorded_at=_T0,
                )
                await record_moderation_audit(session, **_kwargs())
        assert await list_audit_entries(session) == []

    async def test_concurrent_operators_keep_one_chain(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        first_written = asyncio.Event()
        release_first = asyncio.Event()

        async def append_one(index: int) -> None:
            async with session_maker() as session, session.begin():
                await record_moderation_audit(
                    session,
                    **_kwargs(miner_hotkey=f"5Miner{index}", recorded_at=_T0),
                )
                if index == 0:
                    first_written.set()
                    await release_first.wait()

        first = asyncio.create_task(append_one(0))
        await asyncio.wait_for(first_written.wait(), timeout=5)
        second = asyncio.create_task(append_one(1))
        try:
            completed, _ = await asyncio.wait({second}, timeout=0.2)
            assert not completed, "a concurrent append bypassed the audit-chain lock"
        finally:
            release_first.set()
        await asyncio.gather(first, second)
        async with session_maker() as session:
            entries = await list_audit_entries(session)
        assert len(entries) == 2
        assert verify_audit_chain(entries) is True
        assert all(verify_moderation_payload(row.payload) for row in entries)

    async def test_reversal_appends_and_links_the_prior_action(
        self, session: AsyncSession
    ) -> None:
        agent_id = uuid4()
        async with session.begin():
            rejected = await record_moderation_audit(
                session, **_kwargs(agent_id=agent_id)
            )
        async with session.begin():
            released = await record_moderation_audit(
                session,
                **_kwargs(
                    agent_id=agent_id,
                    action_type=ACTION_RELEASE,
                    previous_status="rejected",
                    resulting_status="evaluating",
                    related_action_id=rejected.payload["action_id"],
                ),
            )
        assert released.payload["related_action_id"] == rejected.payload["action_id"]
        assert rejected.payload["action_type"] == ACTION_REJECT
        entries = await list_audit_entries(session)
        assert verify_audit_chain(entries) is True
        assert entries[0].payload == rejected.payload

    async def test_redaction_drops_private_fields(self) -> None:
        private = {
            "schema_version": 1,
            "action_id": str(uuid4()),
            "action_type": ACTION_REJECT,
            "reason_code": "operator_reject",
            "notes": "reviewer saw the hidden prompt",
            "evidence": {"source": "secret.rs"},
            "token": "bearer-secret",
            "operator_email": "ops@example.com",
            "reason": "free-text reviewer note",
            "signature": "ab",
        }
        public = redact_moderation_payload(private)
        assert "notes" not in public
        assert "evidence" not in public
        assert "token" not in public
        assert "operator_email" not in public
        assert "reason" not in public
        assert public["reason_code"] == "operator_reject"
        assert verify_moderation_payload({**public, "notes": "secret"}) is False

    async def test_signer_rotation_verifies_old_and_new_events(
        self, session: AsyncSession
    ) -> None:
        first = Ed25519PrivateKey.generate()
        second = Ed25519PrivateKey.generate()
        configure_moderation_signer(first)
        async with session.begin():
            older = await record_moderation_audit(session, **_kwargs())
        first_public = published_signer_public_keys()[0]
        configure_moderation_signer(second, previous=(bytes.fromhex(first_public),))
        async with session.begin():
            newer = await record_moderation_audit(
                session, **_kwargs(action_type=ACTION_RELEASE)
            )
        trusted = published_signer_public_keys()
        assert older.payload["signer_key_id"] != newer.payload["signer_key_id"]
        assert verify_moderation_payload(older.payload, trusted_public_keys=trusted)
        assert verify_moderation_payload(newer.payload, trusted_public_keys=trusted)
        assert (
            verify_moderation_payload(older.payload, trusted_public_keys=[trusted[0]])
            is False
        )

    def test_preview_hash_is_stable_and_omits_reviewer_text(self) -> None:
        code, digest = preview_moderation_record(
            action_type=ACTION_RESCREEN,
            artifact_sha256=_ARTIFACT,
            screened_image_sha256=None,
            previous_status="quarantined",
            resulting_status="screening_failed",
        )
        again, again_digest = preview_moderation_record(
            action_type=ACTION_RESCREEN,
            artifact_sha256=_ARTIFACT,
            screened_image_sha256=None,
            previous_status="quarantined",
            resulting_status="screening_failed",
        )
        assert code == again == "operator_rescreen"
        assert digest == again_digest
        assert len(digest) == 64
