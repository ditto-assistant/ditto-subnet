from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from ditto.api_server.coding_hosted_private_grants import HostedPrivateGrantStore
from ditto.coding_hosted_private import HostedTaskSelection
from ditto.db.models import Agent, CodingHostedPrivateTask
from ditto.db.queries.coding_hosted_admission import (
    create_hosted_assignment,
    start_hosted_attempt,
)
from ditto.db.queries.coding_hosted_private import (
    HostedPatchFreeze,
    HostedPrivateTaskError,
    bind_hosted_private_task,
    close_hosted_private_task,
    freeze_hosted_private_patch,
)
from ditto.db.queries.coding_private_v2_releases import append_private_v2_release_event
from ditto.tests.db.queries.test_coding_hosted_admission import _admit, _request, _seed


async def _prepared(
    maker, *, start=True, bind=True, deadline=None, registration_bundle=None
):
    authority = await _seed(
        maker, approve=False, registration_bundle=registration_bundle
    )
    selection = HostedTaskSelection(
        authority.evaluation_id,
        authority.attempt_id,
        authority.registration_sha256,
        authority.artifact_sha256,
        "e" * 64,
        7,
        1024,
    )
    authority = replace(
        authority,
        selection_sha256=selection.digest(),
        deadline_unix=deadline or authority.deadline_unix,
    )
    async with maker() as session, session.begin():
        await create_hosted_assignment(
            session,
            authority=authority,
            confirmed_assignment_sha256=authority.digest(),
            actor="test",
            reason="synthetic shadow test",
        )
        grants = (
            await bind_hosted_private_task(session, selection=selection)
            if bind
            else None
        )
    worker = uuid4()
    if start:
        await _admit(maker, _request(authority))
        async with maker() as session, session.begin():
            await start_hosted_attempt(
                session,
                evaluation_id=authority.evaluation_id,
                expected_attempt_id=authority.attempt_id,
                worker_id=worker,
            )
    return authority, selection, worker, grants


def _store(maker, worker, audience="platform-authoring"):
    return HostedPrivateGrantStore(sessions=maker, worker_id=worker, audience=audience)


async def _freeze(maker, authority, worker, patch=b"synthetic private patch"):
    async with maker() as session, session.begin():
        return await freeze_hosted_private_patch(
            session,
            evaluation_id=authority.evaluation_id,
            attempt_id=authority.attempt_id,
            worker_id=worker,
            patch=patch,
        )


async def _close(maker, authority, worker):
    async with maker() as session, session.begin():
        return await close_hosted_private_task(
            session,
            evaluation_id=authority.evaluation_id,
            attempt_id=authority.attempt_id,
            worker_id=worker,
            reason="aborted",
        )


async def test_grants_switch_only_after_committed_freeze_and_close(session_maker):
    authority, _, worker, grants = await _prepared(session_maker)
    authoring = _store(session_maker, worker)
    grading = _store(session_maker, worker, "platform-grading")
    grant = await authoring.active_grant(
        grant_id=grants.authoring_grant_id, audience="platform-authoring"
    )
    assert (
        grant is not None
        and grant.catalog_index == 7
        and grant.attempt_id == authority.attempt_id
    )
    assert (
        "grader_bundle" not in grant.allowed_roles
        and "memory_bundle" in grant.allowed_roles
    )
    assert (
        await grading.active_grant(
            grant_id=grants.grading_grant_id, audience="platform-grading"
        )
        is None
    )
    frozen = await _freeze(session_maker, authority, worker)
    assert frozen.newly_frozen
    assert frozen.patch_sha256 == hashlib.sha256(b"synthetic private patch").hexdigest()
    assert (
        await authoring.active_grant(
            grant_id=grants.authoring_grant_id, audience="platform-authoring"
        )
        is None
    )
    grant = await grading.active_grant(
        grant_id=grants.grading_grant_id, audience="platform-grading"
    )
    assert grant is not None and grant.frozen_patch_sha256 == frozen.patch_sha256
    assert (
        "grader_bundle" in grant.allowed_roles
        and "memory_bundle" not in grant.allowed_roles
    )
    assert await _close(session_maker, authority, worker)
    assert not await _close(session_maker, authority, worker)
    assert (
        await grading.active_grant(
            grant_id=grants.grading_grant_id, audience="platform-grading"
        )
        is None
    )
    assert not (await _freeze(session_maker, authority, worker)).newly_frozen


async def test_selection_is_preapproved_immutable_and_bound_before_start(session_maker):
    authority, selection, worker, grants = await _prepared(session_maker, start=False)
    async with session_maker() as session, session.begin():
        replay = await bind_hosted_private_task(session, selection=selection)
        assert replay == grants
    async with session_maker() as session, session.begin():
        with pytest.raises(HostedPrivateTaskError, match="does not match"):
            await bind_hosted_private_task(
                session, selection=replace(selection, catalog_index=8)
            )
    assert (
        await _store(session_maker, worker).active_grant(
            grant_id=grants.authoring_grant_id, audience="platform-authoring"
        )
        is None
    )
    with pytest.raises(HostedPrivateTaskError, match="worker"):
        await _freeze(session_maker, authority, worker)


async def test_late_binding_is_denied(session_maker):
    _, selection, _, _ = await _prepared(session_maker, bind=False)
    async with session_maker() as session, session.begin():
        with pytest.raises(HostedPrivateTaskError, match="binding is closed"):
            await bind_hosted_private_task(session, selection=selection)


async def test_wrong_worker_audience_and_grant_are_denied(session_maker):
    authority, _, worker, grants = await _prepared(session_maker)
    assert (
        await _store(session_maker, uuid4()).active_grant(
            grant_id=grants.authoring_grant_id, audience="platform-authoring"
        )
        is None
    )
    assert (
        await _store(session_maker, worker).active_grant(
            grant_id=grants.grading_grant_id, audience="platform-grading"
        )
        is None
    )
    assert (
        await _store(session_maker, worker).active_grant(
            grant_id=uuid4(), audience="platform-authoring"
        )
        is None
    )
    with pytest.raises(HostedPrivateTaskError, match="worker"):
        await _freeze(session_maker, authority, uuid4())
    with pytest.raises(HostedPrivateTaskError, match="worker"):
        await _close(session_maker, authority, uuid4())


async def test_freeze_rollback_retains_authoring_and_cannot_replace_patch(
    session_maker,
):
    authority, _, worker, grants = await _prepared(session_maker)
    with pytest.raises(RuntimeError):
        async with session_maker() as session, session.begin():
            await freeze_hosted_private_patch(
                session,
                evaluation_id=authority.evaluation_id,
                attempt_id=authority.attempt_id,
                worker_id=worker,
                patch=b"first",
            )
            raise RuntimeError("synthetic rollback")
    assert (
        await _store(session_maker, worker).active_grant(
            grant_id=grants.authoring_grant_id, audience="platform-authoring"
        )
        is not None
    )
    assert (await _freeze(session_maker, authority, worker, b"second")).newly_frozen
    assert not (await _freeze(session_maker, authority, worker, b"second")).newly_frozen
    with pytest.raises(HostedPrivateTaskError, match="conflicts"):
        await _freeze(session_maker, authority, worker, b"first")


async def test_concurrent_freezes_accept_only_one_identity(session_maker):
    authority, _, worker, _ = await _prepared(session_maker)
    results = await asyncio.gather(
        _freeze(session_maker, authority, worker, b"one"),
        _freeze(session_maker, authority, worker, b"two"),
        return_exceptions=True,
    )
    assert (
        sum(isinstance(r, HostedPatchFreeze) and r.newly_frozen for r in results) == 1
    )
    assert sum(isinstance(r, HostedPrivateTaskError) for r in results) == 1


@pytest.mark.parametrize("invalidate", ["retirement", "artifact"])
async def test_lifecycle_revokes_access_but_does_not_block_cleanup(
    session_maker, invalidate
):
    authority, _, worker, grants = await _prepared(session_maker)
    async with session_maker() as session, session.begin():
        if invalidate == "retirement":
            await append_private_v2_release_event(
                session,
                corpus_release_id="private-coding-v2-release-001",
                expected_registration_sha256=authority.registration_sha256,
                action="retired",
                actor="test",
                reason="synthetic retirement",
            )
        else:
            await session.execute(
                update(Agent)
                .where(Agent.agent_id == authority.agent_id)
                .values(sha256="f" * 64)
            )
    assert (
        await _store(session_maker, worker).active_grant(
            grant_id=grants.authoring_grant_id, audience="platform-authoring"
        )
        is None
    )
    assert await _close(session_maker, authority, worker)


async def test_aborted_task_cannot_freeze_or_reopen(session_maker):
    authority, _, worker, grants = await _prepared(session_maker)
    await _close(session_maker, authority, worker)
    with pytest.raises(HostedPrivateTaskError, match="closed"):
        await _freeze(session_maker, authority, worker)
    assert (
        await _store(session_maker, worker).active_grant(
            grant_id=grants.authoring_grant_id, audience="platform-authoring"
        )
        is None
    )


async def test_sql_cannot_rewrite_authority_or_bypass_phase_guards(session_maker):
    authority, _, worker, _ = await _prepared(session_maker, start=False)
    changes = [
        {"catalog_index": 8},
        {"authoring_grant_id": uuid4()},
        {"frozen_patch_sha256": "f" * 64},
        {"close_reason": "aborted"},
        {
            "frozen_at": datetime.now(UTC),
            "frozen_patch_sha256": "f" * 64,
            "frozen_patch_size": 1,
        },
    ]
    for values in changes:
        with pytest.raises(IntegrityError):
            async with session_maker() as session, session.begin():
                await session.execute(
                    update(CodingHostedPrivateTask)
                    .where(
                        CodingHostedPrivateTask.evaluation_id == authority.evaluation_id
                    )
                    .values(**values)
                )
    with pytest.raises(IntegrityError):
        async with session_maker() as session, session.begin():
            await session.execute(
                delete(CodingHostedPrivateTask).where(
                    CodingHostedPrivateTask.evaluation_id == authority.evaluation_id
                )
            )
    await _admit(session_maker, _request(authority))
    async with session_maker() as session, session.begin():
        await start_hosted_attempt(
            session,
            evaluation_id=authority.evaluation_id,
            expected_attempt_id=authority.attempt_id,
            worker_id=worker,
        )
    await _freeze(session_maker, authority, worker)
    for values in [
        {"frozen_at": None, "frozen_patch_sha256": None, "frozen_patch_size": None},
        {"frozen_patch_sha256": "f" * 64},
    ]:
        with pytest.raises(IntegrityError):
            async with session_maker() as session, session.begin():
                await session.execute(
                    update(CodingHostedPrivateTask)
                    .where(
                        CodingHostedPrivateTask.evaluation_id == authority.evaluation_id
                    )
                    .values(**values)
                )
    await _close(session_maker, authority, worker)
    with pytest.raises(IntegrityError):
        async with session_maker() as session, session.begin():
            await session.execute(
                update(CodingHostedPrivateTask)
                .where(CodingHostedPrivateTask.evaluation_id == authority.evaluation_id)
                .values(closed_at=None, close_reason=None)
            )


@pytest.mark.parametrize(
    "change",
    [
        {"catalog_index": True},
        {"catalog_index": 250},
        {"max_patch_bytes": 0},
        {"max_patch_bytes": True},
        {"attempt_id": UUID(int=0)},
        {"schedule_sha256": "PRIVATE_MARKER"},
    ],
)
def test_selection_bounds(change):
    selection = HostedTaskSelection(
        uuid4(), uuid4(), "a" * 64, "b" * 64, "c" * 64, 0, 1024
    )
    with pytest.raises(ValueError) as caught:
        replace(selection, **change).digest()
    assert "PRIVATE_MARKER" not in str(caught.value)


async def test_patch_bytes_are_bounded_before_freeze(session_maker):
    authority, _, worker, _ = await _prepared(session_maker)
    with pytest.raises(HostedPrivateTaskError, match="invalid"):
        await _freeze(session_maker, authority, worker, b"x" * 1025)
    assert (await _freeze(session_maker, authority, worker, b"")).patch_size_bytes == 0


async def test_database_clock_expiry_denies_grant_and_new_freeze(
    session_maker, monkeypatch
):
    authority, _, worker, grants = await _prepared(session_maker)

    async def expired(_):
        return datetime.fromtimestamp(authority.deadline_unix, UTC)

    with monkeypatch.context() as patcher:
        patcher.setattr("ditto.db.queries.coding_hosted_private._now", expired)
        assert (
            await _store(session_maker, worker).active_grant(
                grant_id=grants.authoring_grant_id, audience="platform-authoring"
            )
            is None
        )
        with pytest.raises(HostedPrivateTaskError, match="closed"):
            await _freeze(session_maker, authority, worker)


async def test_close_and_freeze_race_never_reopens_authoring(session_maker):
    authority, _, worker, grants = await _prepared(session_maker)
    results = await asyncio.gather(
        _freeze(session_maker, authority, worker),
        _close(session_maker, authority, worker),
        return_exceptions=True,
    )
    assert results[1] is True
    assert isinstance(results[0], (HostedPatchFreeze, HostedPrivateTaskError))
    assert (
        await _store(session_maker, worker).active_grant(
            grant_id=grants.authoring_grant_id, audience="platform-authoring"
        )
        is None
    )
    assert (
        await _store(session_maker, worker, "platform-grading").active_grant(
            grant_id=grants.grading_grant_id, audience="platform-grading"
        )
        is None
    )


@pytest.mark.parametrize("corruption", ["index", "artifact", "malformed_uuid"])
async def test_grant_checks_selection_preimage_not_just_stored_digest(
    session_maker, corruption
):
    authority, selection, worker, _ = await _prepared(
        session_maker, start=False, bind=False
    )
    projection = selection.projection()
    if corruption == "artifact":
        projection["artifact_sha256"] = "f" * 64
    if corruption == "malformed_uuid":
        projection["attempt_id"] = []
    grant_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            CodingHostedPrivateTask(
                evaluation_id=authority.evaluation_id,
                selection_sha256=selection.digest(),
                selection_authority=projection,
                catalog_index=8 if corruption == "index" else 7,
                max_patch_bytes=selection.max_patch_bytes,
                authoring_grant_id=grant_id,
                grading_grant_id=uuid4(),
            )
        )
    await _admit(session_maker, _request(authority))
    async with session_maker() as session, session.begin():
        await start_hosted_attempt(
            session,
            evaluation_id=authority.evaluation_id,
            expected_attempt_id=authority.attempt_id,
            worker_id=worker,
        )
    assert (
        await _store(session_maker, worker).active_grant(
            grant_id=grant_id, audience="platform-authoring"
        )
        is None
    )
    with pytest.raises(HostedPrivateTaskError, match="selection is invalid"):
        await _freeze(session_maker, authority, worker)
