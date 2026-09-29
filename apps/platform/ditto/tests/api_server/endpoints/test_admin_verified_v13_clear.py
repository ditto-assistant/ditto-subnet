"""A held v13 court clear is released only on its re-verified signed receipt."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener_review_settings import ScreenerReviewSettings
from ditto.api_server.endpoints.screener import _review_settings_checksum
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    ScreenerPolicyActivation,
    ScreenerReviewSettingsRevision,
    ScreeningAttempt,
    ScreeningQuarantine,
    ScreeningQuarantineResolution,
    ScreeningReviewEvent,
)
from ditto.tests.api_server.endpoints.test_screener import (
    _ADMIN_HEADERS,
    _AUTH_HEADER,
    _SCREENER_HOTKEY,
    _SHA256,
    _FakeGenerator,
    _install_chain,
    _install_db,
    _install_generator,
    _result_payload,
    _seed_agent,
    _seed_hetzner_primary,
    _sign,
)
from ditto_screening_protocol import (
    AdjudicationCompletionReceipt,
    SourceReviewAdjudication,
    SourceReviewInvariant,
    completion_receipt_signing_message,
)

AdjudicatorMode = Literal["off", "shadow", "enforce"]

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_AWAITING = "source-review-awaiting-v13-verification"
_CONFIRMATION = "RELEASE VERIFIED V13 COURT CLEAR"
_REASON = "Court clear receipt re-verified; admit for scoring"
_RECEIPT = AdjudicationCompletionReceipt(
    elapsed_ms=4300,
    first_tool_call_ms=2000,
    first_tool_observation="stream_delta",
    observed_model="z-ai/glm-5.3-flash",
    gateway_provider="openrouter",
    observed_upstream="together",
    request_count=1,
    final_request_prompt_bytes=8000,
    final_request_wire_bytes=700,
    final_request_event_count=4,
    prompt_tokens=200,
    completion_tokens=80,
)


@pytest.fixture(autouse=True)
def _admin_api(app: FastAPI, session_maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    _install_db(app, session_maker)
    _install_chain(app)


def _adjudication(
    decision: str = "clear",
    *,
    receipt: bool = True,
    policy_version: int = 13,
    prompt_revision: str | None = None,
) -> SourceReviewAdjudication:
    basis: dict[str, object] = {
        "clear": {"clear_clause": "model_authors_graded_slot"},
        "reject": {"reject_invariant": SourceReviewInvariant.EVALUATION_INDEPENDENCE},
        "escalate": {"escalation_code": "verdict-contract-failed"},
    }[decision]
    return SourceReviewAdjudication.model_validate(
        {
            "decision": decision,
            "reason": "The served model authors the graded response at src/main.rs:6.",
            "citations": [{"path": "src/main.rs", "line": 6}],
            "notes_considered": 1,
            "model": "z-ai/glm-5.3-flash",
            "prompt_revision": prompt_revision
            or f"adjudicator-v7-policy-v{policy_version}",
            "policy_version": policy_version,
            "completion_receipt": _RECEIPT if receipt else None,
            **basis,
        }
    )


def _receipt_signature(
    agent_id: UUID, attempt_id: UUID, adjudication: SourceReviewAdjudication
) -> str:
    assert adjudication.completion_receipt is not None
    return _sign(
        completion_receipt_signing_message(
            screener_hotkey=_SCREENER_HOTKEY,
            agent_id=agent_id,
            attempt_id=attempt_id,
            artifact_sha256=_SHA256,
            adjudication_digest=adjudication.canonical_digest(),
            receipt=adjudication.completion_receipt,
        )
    )


async def _settings_revision(
    session: AsyncSession, adjudicator_mode: AdjudicatorMode = "enforce"
) -> tuple[int, str]:
    settings = ScreenerReviewSettings(mode="enforce", adjudicator_mode=adjudicator_mode)
    checksum = _review_settings_checksum(settings)
    revision = ScreenerReviewSettingsRevision(
        parent_revision=0,
        scope="*",
        settings=settings.model_dump(mode="json"),
        checksum=checksum,
        reason="v13 court posture",
        actor="test",
    )
    session.add(revision)
    await session.flush()
    return revision.revision, checksum


def _release_url(quarantine_id: UUID | str) -> str:
    return (
        f"/api/v1/admin/screening-quarantines/{quarantine_id}"
        "/release-verified-v13-clear"
    )


def _release_body(**overrides: object) -> dict[str, object]:
    return {
        "reason": _REASON,
        "expected_sha256": _SHA256,
        "confirmation": _CONFIRMATION,
        **overrides,
    }


async def _hold_worker_clear(
    client: httpx.AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> tuple[UUID, UUID]:
    """Post the worker's exact v13 court clear and return the held quarantine."""
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=6,
                desired_version=7,
                status="activated",
                cohort_size=5,
                created_at=now - timedelta(hours=1),
                activated_at=now - timedelta(minutes=30),
            )
        )
    agent_id = await _seed_agent(session_maker, status=AgentStatus.SCREENING)
    attempt_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerPolicyActivation(
                parent_revision=0,
                target_policy_version=13,
                activate_at=now - timedelta(minutes=1),
                rescreen_scored=False,
                reason="v13 is the required screening policy",
                actor="test",
            )
        )
        revision_id, checksum = await _settings_revision(session)
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                artifact_sha256=_SHA256,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=13,
                status="running",
                started_at=now - timedelta(minutes=1),
                deadline=now + timedelta(minutes=9),
                review_settings_revision=revision_id,
                review_settings_instance_id="ditto-screener-prod",
                review_settings_scope="*",
                review_settings_checksum=checksum,
            )
        )
    adjudication = _adjudication()
    held = await client.post(
        f"/api/v1/screener/agent/{agent_id}/result",
        headers=_AUTH_HEADER,
        json=_result_payload(
            agent_id,
            passed=False,
            policy_version=13,
            attempt_id=attempt_id,
            outcome="quarantine",
            manifest_digest="12" * 32,
            reason_code=_AWAITING,
            evidence=[
                {
                    "module_id": "luna-source-review",
                    "code": "source-review-adjudicated",
                    "summary": "final source-review adjudication completed",
                },
                {
                    "module_id": "luna-source-review",
                    "code": _AWAITING,
                    "summary": "source adjudication held pending v13 verification",
                },
            ],
            review_settings_revision=revision_id,
            review_settings_instance_id="ditto-screener-prod",
            review_settings_scope="*",
            review_settings_checksum=checksum,
            adjudication_digest=adjudication.canonical_digest(),
            adjudication=adjudication.model_dump(mode="json"),
            completion_receipt_signature=_receipt_signature(
                agent_id, attempt_id, adjudication
            ),
        ),
    )
    assert held.status_code == 200, held.text
    assert held.json()["status"] == AgentStatus.QUARANTINED
    async with session_maker() as session:
        quarantine_id = await session.scalar(
            select(ScreeningQuarantine.quarantine_id).where(
                ScreeningQuarantine.attempt_id == attempt_id
            )
        )
    assert quarantine_id is not None
    return agent_id, quarantine_id


async def test_worker_court_clear_is_released_to_evaluation_on_its_receipt(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Worker clear -> held quarantine -> verified release -> build-only claim."""
    generator = _FakeGenerator()
    _install_generator(app, generator)
    agent_id, quarantine_id = await _hold_worker_clear(client, session_maker)

    listing = await client.get(
        "/api/v1/admin/screening-quarantines", headers=_ADMIN_HEADERS
    )
    assert listing.status_code == 200, listing.text
    [row] = listing.json()["items"]
    assert row["quarantine_id"] == str(quarantine_id)
    assert row["screening_reason_code"] == "adjudicated-source-review-clear"
    assert _AWAITING in {item["code"] for item in row["evidence"]}

    url = _release_url(quarantine_id)
    unauthenticated = await client.post(url, json=_release_body())
    no_actor = await client.post(
        url,
        headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
        json=_release_body(),
    )
    unconfirmed = await client.post(
        url,
        headers=_ADMIN_HEADERS,
        json={"reason": _REASON, "expected_sha256": _SHA256},
    )
    misconfirmed = await client.post(
        url,
        headers=_ADMIN_HEADERS,
        json=_release_body(confirmation="RELEASE QUARANTINE"),
    )
    other_artifact = await client.post(
        url, headers=_ADMIN_HEADERS, json=_release_body(expected_sha256="cd" * 32)
    )
    unknown = await client.post(
        _release_url(uuid4()), headers=_ADMIN_HEADERS, json=_release_body()
    )
    assert unauthenticated.status_code == 401, unauthenticated.text
    assert no_actor.status_code == 422, no_actor.text
    assert unconfirmed.status_code == 422, unconfirmed.text
    assert misconfirmed.status_code == 422, misconfirmed.text
    assert other_artifact.status_code == 409, other_artifact.text
    assert other_artifact.json()["message"] == "artifact identity changed"
    assert unknown.status_code == 404, unknown.text
    assert generator.calls == 0

    released = await client.post(url, headers=_ADMIN_HEADERS, json=_release_body())
    replay = await client.post(url, headers=_ADMIN_HEADERS, json=_release_body())
    other_reason = await client.post(
        url,
        headers=_ADMIN_HEADERS,
        json=_release_body(reason="A second, different release reason"),
    )
    generic = await client.post(
        f"/api/v1/admin/screening-quarantines/{quarantine_id}/resolve",
        headers=_ADMIN_HEADERS,
        json={"resolution": "reject", "reason": "Later conflicting ruling"},
    )

    assert released.status_code == 200, released.text
    body = released.json()
    assert body["agent_status"] == AgentStatus.EVALUATING
    assert body["idempotent"] is False
    assert body["quarantine"]["resolution"] == "release"
    assert body["quarantine"]["resolution_reason_code"] == (
        "operator-released-quarantine"
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent"] is True
    assert replay.json()["adjudication_digest"] == body["adjudication_digest"]
    assert other_reason.status_code == 409, other_reason.text
    assert other_reason.json()["message"] == "quarantine is not active"
    assert generic.status_code == 409, generic.text
    assert generator.calls == 1

    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.EVALUATING
        assert agent.screening_policy_version == 13
        assert agent.screening_reason == _REASON
        assert agent.screened_image_sha256 is None
        assert await session.scalar(
            select(func.count())
            .select_from(BenchmarkDataset)
            .where(BenchmarkDataset.agent_id == agent_id)
        )
        resolutions = (
            await session.scalars(
                select(ScreeningQuarantineResolution).where(
                    ScreeningQuarantineResolution.quarantine_id == quarantine_id
                )
            )
        ).all()
        assert [(r.resolution, r.actor) for r in resolutions] == [
            ("release", "backroom:test-user")
        ]
        manual = (
            await session.scalars(
                select(ScreeningReviewEvent).where(
                    ScreeningReviewEvent.agent_id == agent_id,
                    ScreeningReviewEvent.event_kind == "manual",
                )
            )
        ).all()
        assert len(manual) == 1
        assert manual[0].actor == "backroom:test-user"
        assert manual[0].effective_decision == "release"
        assert manual[0].prior_agent_status == AgentStatus.QUARANTINED
        assert manual[0].next_agent_status == AgentStatus.EVALUATING
        verified = manual[0].evidence["verified_v13_court_clear"]
        assert verified["adjudication_digest"] == body["adjudication_digest"]
        assert verified["completion_receipt_signer"] == _SCREENER_HOTKEY

    # The same state an ordinary PASS reaches, minus the image the hold never
    # uploaded: the fail-closed build-only lane rebuilds it before scoring.
    await _seed_hetzner_primary(session_maker)
    claimed = await client.post(
        "/api/v1/screener/claim?policy_version=13", headers=_AUTH_HEADER
    )
    assert claimed.status_code == 200, claimed.text
    [item] = claimed.json()["items"]
    assert item["agent_id"] == str(agent_id)
    assert item["build_only"] is True


def _drop_awaiting(_: ScreeningAttempt, q: ScreeningQuarantine, __: object) -> None:
    q.evidence = [item for item in q.evidence or [] if item["code"] != _AWAITING]


def _other_artifact(a: ScreeningAttempt, _: object, __: object) -> None:
    a.artifact_sha256 = "cd" * 32


def _other_review_scope(a: ScreeningAttempt, _: object, __: object) -> None:
    a.review_settings_scope = "other-screener"


def _tamper_adjudication(_: object, __: object, e: ScreeningReviewEvent) -> None:
    e.evidence = {
        **e.evidence,
        "adjudication": {**e.evidence["adjudication"], "reason": "Edited later."},
    }


def _other_receipt(_: object, q: ScreeningQuarantine, __: object) -> None:
    q.court_completion_receipt = {
        **(q.court_completion_receipt or {}),
        "request_count": 2,
    }


def _drop_signature(_: object, __: object, e: ScreeningReviewEvent) -> None:
    e.evidence = {
        key: value
        for key, value in e.evidence.items()
        if key != "completion_receipt_signature"
    }


def _forge_signature(_: object, __: object, e: ScreeningReviewEvent) -> None:
    e.evidence = {**e.evidence, "completion_receipt_signature": _sign(b"other")}


def _resolved(_: object, q: ScreeningQuarantine, __: object) -> None:
    q.status = "resolved"
    q.resolution = "rescreen"
    q.resolved_by = "backroom:other"
    q.resolution_reason = "Rescreened before the verified release"
    q.resolved_at = datetime.now(UTC)


Edit = Callable[[ScreeningAttempt, ScreeningQuarantine, ScreeningReviewEvent], None]


async def _seed_held_clear(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    policy_version: int = 13,
    decision: str = "clear",
    adjudicator_mode: AdjudicatorMode = "enforce",
    receipt: bool = True,
    adjudication_policy_version: int = 13,
    adjudication_prompt_revision: str | None = None,
    edit: Edit | None = None,
) -> tuple[UUID, UUID]:
    """Seed one held court decision exactly as the verdict path stores it."""
    agent_id = await _seed_agent(session_maker, status=AgentStatus.QUARANTINED)
    adjudication = _adjudication(
        decision,
        receipt=receipt,
        policy_version=adjudication_policy_version,
        prompt_revision=adjudication_prompt_revision,
    )
    attempt_id = uuid4()
    quarantine_id = uuid4()
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        revision_id, checksum = await _settings_revision(session, adjudicator_mode)
        attempt = ScreeningAttempt(
            attempt_id=attempt_id,
            agent_id=agent_id,
            artifact_sha256=_SHA256,
            screener_hotkey=_SCREENER_HOTKEY,
            policy_version=policy_version,
            status="quarantined",
            started_at=now - timedelta(minutes=2),
            finished_at=now - timedelta(minutes=1),
            deadline=now + timedelta(minutes=8),
            review_settings_revision=revision_id,
            review_settings_instance_id="ditto-screener-prod",
            review_settings_scope="*",
            review_settings_checksum=checksum,
        )
        quarantine = ScreeningQuarantine(
            quarantine_id=quarantine_id,
            agent_id=agent_id,
            attempt_id=attempt_id,
            screener_hotkey=_SCREENER_HOTKEY,
            policy_version=policy_version,
            manifest_digest="12" * 32,
            reason_code=f"adjudicated-source-review-{decision}",
            evidence=[
                {"module_id": "luna-source-review", "code": _AWAITING, "summary": "."},
                {
                    "module_id": "adjudication",
                    "code": f"adjudicated-source-review-{decision}",
                    "summary": adjudication.reason,
                    "digest": adjudication.canonical_digest(),
                },
            ],
            court_completion_receipt=(
                _RECEIPT.model_dump(mode="json") if receipt else None
            ),
            status="active",
        )
        event = ScreeningReviewEvent(
            event_id=uuid4(),
            agent_id=agent_id,
            attempt_id=attempt_id,
            quarantine_id=quarantine_id,
            event_kind="automated",
            artifact_sha256=_SHA256,
            policy_version=policy_version,
            actor=f"screener:{_SCREENER_HOTKEY}",
            outcome="quarantine",
            effective_decision="hold",
            prior_agent_status=AgentStatus.SCREENING,
            next_agent_status=AgentStatus.QUARANTINED,
            evidence={
                "adjudication_digest": adjudication.canonical_digest(),
                "adjudication": adjudication.model_dump(mode="json"),
                "completion_receipt_signature": (
                    _receipt_signature(agent_id, attempt_id, adjudication)
                    if receipt
                    else None
                ),
            },
            created_at=now,
        )
        if edit is not None:
            edit(attempt, quarantine, event)
        session.add(attempt)
        await session.flush()
        session.add(quarantine)
        await session.flush()
        session.add(event)

    return agent_id, quarantine_id


@pytest.mark.parametrize(
    ("policy_version", "decision", "adjudicator_mode", "receipt", "edit", "message"),
    [
        pytest.param(
            12,
            "clear",
            "enforce",
            True,
            None,
            "only a policy v13 court clear can be released this way",
            id="policy-12",
        ),
        pytest.param(
            13,
            "reject",
            "enforce",
            True,
            None,
            "quarantine is not a v13 court clear awaiting verification",
            id="reject",
        ),
        pytest.param(
            13,
            "escalate",
            "enforce",
            True,
            None,
            "quarantine is not a v13 court clear awaiting verification",
            id="escalate",
        ),
        pytest.param(
            13,
            "clear",
            "enforce",
            True,
            _drop_awaiting,
            "quarantine is not a v13 court clear awaiting verification",
            id="not-awaiting-verification",
        ),
        pytest.param(
            13,
            "clear",
            "enforce",
            True,
            _other_artifact,
            "court clear is not bound to this artifact",
            id="other-artifact",
        ),
        pytest.param(
            13,
            "clear",
            "shadow",
            True,
            None,
            "court clear was not produced under an enforced adjudicator",
            id="shadow-adjudicator",
        ),
        pytest.param(
            13,
            "clear",
            "enforce",
            True,
            _other_review_scope,
            "court clear was not produced under an enforced adjudicator",
            id="other-review-scope",
        ),
        pytest.param(
            13,
            "clear",
            "enforce",
            True,
            _tamper_adjudication,
            "retained adjudication does not match its signed digest",
            id="tampered-adjudication",
        ),
        pytest.param(
            13,
            "clear",
            "enforce",
            False,
            None,
            "court completion receipt was not retained",
            id="missing-receipt",
        ),
        pytest.param(
            13,
            "clear",
            "enforce",
            True,
            _other_receipt,
            "court completion receipt was not retained",
            id="mismatched-receipt",
        ),
        pytest.param(
            13,
            "clear",
            "enforce",
            True,
            _drop_signature,
            "court completion receipt was not retained",
            id="missing-signature",
        ),
        pytest.param(
            13,
            "clear",
            "enforce",
            True,
            _forge_signature,
            "court completion receipt signature did not verify",
            id="forged-signature",
        ),
        pytest.param(
            13,
            "clear",
            "enforce",
            True,
            _resolved,
            "quarantine is not active",
            id="not-active",
        ),
    ],
)
async def test_release_refuses_anything_but_a_verified_v13_clear(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    policy_version: int,
    decision: str,
    adjudicator_mode: AdjudicatorMode,
    receipt: bool,
    edit: Edit | None,
    message: str,
) -> None:
    agent_id, quarantine_id = await _seed_held_clear(
        session_maker,
        policy_version=policy_version,
        decision=decision,
        adjudicator_mode=adjudicator_mode,
        receipt=receipt,
        edit=edit,
    )

    refused = await client.post(
        _release_url(quarantine_id), headers=_ADMIN_HEADERS, json=_release_body()
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["message"] == message
    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
        retained = await session.get(ScreeningQuarantine, quarantine_id)
        assert agent is not None and agent.status == AgentStatus.QUARANTINED
        assert retained is not None and retained.resolution != "release"
        assert not await session.scalar(
            select(func.count())
            .select_from(ScreeningReviewEvent)
            .where(
                ScreeningReviewEvent.agent_id == agent_id,
                ScreeningReviewEvent.event_kind == "manual",
            )
        )


@pytest.mark.parametrize(
    ("adjudication_policy_version", "adjudication_prompt_revision"),
    [
        (12, None),
        (13, "adjudicator-v7-policy-v12"),
    ],
)
async def test_release_refuses_signed_older_policy_adjudication(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    adjudication_policy_version: int,
    adjudication_prompt_revision: str | None,
) -> None:
    agent_id, quarantine_id = await _seed_held_clear(
        session_maker,
        policy_version=13,
        adjudication_policy_version=adjudication_policy_version,
        adjudication_prompt_revision=adjudication_prompt_revision,
    )

    refused = await client.post(
        _release_url(quarantine_id), headers=_ADMIN_HEADERS, json=_release_body()
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["message"] == "court adjudication policy mismatch"
    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
        retained = await session.get(ScreeningQuarantine, quarantine_id)
        assert agent is not None and agent.status == AgentStatus.QUARANTINED
        assert retained is not None and retained.status == "active"


async def test_release_refuses_a_hold_superseded_by_a_later_attempt(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id, quarantine_id = await _seed_held_clear(session_maker)
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        # Same artifact, screened again after the hold was recorded.
        session.add(
            ScreeningAttempt(
                attempt_id=uuid4(),
                agent_id=agent_id,
                artifact_sha256=_SHA256,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=13,
                status="expired",
                started_at=now,
                finished_at=now,
                deadline=now,
            )
        )

    refused = await client.post(
        _release_url(quarantine_id), headers=_ADMIN_HEADERS, json=_release_body()
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["message"] == (
        "a later screening attempt supersedes this hold"
    )
    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
        retained = await session.get(ScreeningQuarantine, quarantine_id)
        assert agent is not None and agent.status == AgentStatus.QUARANTINED
        assert retained is not None and retained.status == "active"
