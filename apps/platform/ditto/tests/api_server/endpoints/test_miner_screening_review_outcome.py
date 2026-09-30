"""Bounded source-review outcome on owner screening feedback (#1249).

The submitter gets an outcome and a neutral next step. The notes ledger, cited
locations, breached invariant, clear clause, court reason text, and refusal code
stay operator-side even when every digest verifies.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.db.models import (
    AgentStatus,
    ScreeningAttempt,
    ScreeningQuarantine,
    ScreeningReviewEvent,
)
from ditto.tests.api_server.endpoints.test_miner_logs import _login
from ditto.tests.api_server.endpoints.test_validator import (
    _SHA256,
    _install_chain,
    _install_db,
    _seed_agent,
)
from ditto_screening_protocol import SourceReviewInvariant
from ditto_screening_protocol.models import (
    AdjudicationCompletionReceipt,
    AdjudicationRunDiagnostic,
    SourceReviewAdjudication,
    SourceReviewNote,
    source_review_notes_digest,
)

_SCREENER = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
_MODEL = "z-ai/glm-5.3-flash"
_PROMPT_REVISION = "adjudicator-v7-policy-v13"
_PUBLIC_REASON = "Submission held for anti-cheat review"
_NOTES = [
    SourceReviewNote(
        kind="concern",
        category="answer_lookup",
        path="src/router.rs",
        line=4242,
        summary="Routes a benchmark-shaped prompt to a lookup table",
        confidence=0.83,
        stage="l2",
    ),
    SourceReviewNote(
        kind="observation",
        category="prompt_shape_probe",
        line=9191,
        summary="Line-only observation about a request classifier",
        stage="l1",
    ),
]
_CLEAR_REASON = "The served model authors the graded response at src/main.rs:6161."
_REJECT_REASON = "The lookup at src/router.rs:4242 answers without the model."
_ESCALATE_REASON = "Automated adjudication did not complete; held for operator review"
# Private review evidence seeded on every record. None of it may reach the
# submitter, even when the ledger and court digests verify.
_PRIVATE_EVIDENCE = (
    # notes ledger: summaries, categories, paths, lines
    "Routes a benchmark-shaped prompt",
    "Line-only observation",
    "answer_lookup",
    "prompt_shape_probe",
    "src/router.rs",
    "src/main.rs",
    # Bare line numbers can collide with random UUID hex, so structured lines
    # are covered by the ``"line"`` key below and cited ones by these.
    "rs:4242",
    "rs:6161",
    # court basis, reason text, refusal
    "i8_evaluation_independence",
    "model_authors_graded_slot",
    _CLEAR_REASON,
    _REJECT_REASON,
    _ESCALATE_REASON,
    "adjudicator-failed",
    # wire keys that would carry any of the above
    '"review_notes"',
    '"adjudication"',
    '"citations"',
    '"path"',
    '"line"',
    '"summary"',
    '"category"',
    '"reject_invariant"',
    '"clear_clause"',
    '"refusal"',
    '"reason"',
    # operator metadata
    _MODEL,
    _PROMPT_REVISION,
    "notes_considered",
    "run_diagnostic",
    "completion_receipt",
    "escalation_code",
    "provider-http-error",
    "openrouter",
    "together",
    "confidence",
    "OPERATOR-FINDING-SUMMARY",
    "read_bytes_used",
    "digest",
    "review_settings_revision",
    "verification_receipts",
)


def _adjudication(decision: str) -> SourceReviewAdjudication:
    basis: dict[str, object] = {
        "clear": {
            "clear_clause": "model_authors_graded_slot",
            "reason": _CLEAR_REASON,
            "citations": [{"path": "src/main.rs", "line": 6161}],
        },
        "reject": {
            "reject_invariant": SourceReviewInvariant.EVALUATION_INDEPENDENCE,
            "reason": _REJECT_REASON,
            "citations": [{"path": "src/router.rs", "line": 4242}],
            "completion_receipt": AdjudicationCompletionReceipt(
                elapsed_ms=4300,
                observed_model=_MODEL,
                gateway_provider="openrouter",
                observed_upstream="together",
                request_count=1,
            ),
        },
        "escalate": {
            "escalation_code": "adjudicator-failed",
            "reason": _ESCALATE_REASON,
            "run_diagnostic": AdjudicationRunDiagnostic(
                error_class="HTTPStatusError",
                failure_code="provider-http-error",
                escalation_code="adjudicator-failed",
                http_status=502,
                elapsed_ms=1200,
                model=_MODEL,
                provider="openrouter",
                upstream="together",
            ),
        },
    }[decision]
    return SourceReviewAdjudication.model_validate(
        {
            "decision": decision,
            "notes_considered": len(_NOTES),
            "model": _MODEL,
            "prompt_revision": _PROMPT_REVISION,
            "policy_version": 13,
            **basis,
        }
    )


async def _seed_review(
    maker: async_sessionmaker[AsyncSession],
    *,
    agent_id: UUID,
    decision: str,
    effective: str = "hold",
    event: bool = True,
    adjudication_digest: str | None = None,
    attempt_artifact_sha256: str | None = _SHA256,
    event_artifact_sha256: str = _SHA256,
    event_policy_version: int = 13,
) -> UUID:
    now = datetime.now(UTC)
    attempt_id = uuid4()
    quarantine_id = uuid4()
    notes_json = [note.model_dump(mode="json") for note in _NOTES]
    notes_digest = source_review_notes_digest(_NOTES)
    adjudication = _adjudication(decision)
    async with maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                artifact_sha256=attempt_artifact_sha256,
                screener_hotkey=_SCREENER,
                policy_version=13,
                status="quarantined",
                started_at=now - timedelta(minutes=5),
                deadline=now + timedelta(minutes=40),
                finished_at=now,
                public_reason=_PUBLIC_REASON,
                reason_code=f"adjudicated-source-review-{decision}",
            )
        )
        await session.flush()
        session.add(
            ScreeningQuarantine(
                quarantine_id=quarantine_id,
                agent_id=agent_id,
                attempt_id=attempt_id,
                screener_hotkey=_SCREENER,
                policy_version=13,
                manifest_digest="12" * 32,
                reason_code=f"adjudicated-source-review-{decision}",
                review_audit={"read_bytes_used": 1234},
                review_audit_digest="34" * 32,
                review_notes=notes_json,
                review_notes_digest=notes_digest,
                finding={"summary": "OPERATOR-FINDING-SUMMARY"},
                status="active",
            )
        )
        await session.flush()
        if event:
            session.add(
                ScreeningReviewEvent(
                    event_id=uuid4(),
                    agent_id=agent_id,
                    attempt_id=attempt_id,
                    quarantine_id=quarantine_id,
                    event_kind="automated",
                    artifact_sha256=event_artifact_sha256,
                    policy_version=event_policy_version,
                    actor=f"screener:{_SCREENER}",
                    reviewer_model=_MODEL,
                    outcome="quarantine",
                    effective_decision=effective,
                    reason_code=f"adjudicated-source-review-{decision}",
                    reason=adjudication.reason,
                    prior_agent_status=AgentStatus.SCREENING,
                    next_agent_status=AgentStatus.QUARANTINED,
                    evidence={
                        "finding": {"summary": "OPERATOR-FINDING-SUMMARY"},
                        "review_audit": {"read_bytes_used": 1234},
                        "review_notes": notes_json,
                        "review_notes_digest": notes_digest,
                        "adjudication": adjudication.model_dump(mode="json"),
                        "adjudication_digest": (
                            adjudication_digest or adjudication.canonical_digest()
                        ),
                        "completion_receipt_signature": "0x" + "ab" * 64,
                        "review_settings_revision": 7,
                        "verification_receipts": [],
                    },
                    created_at=now,
                )
            )
    return attempt_id


async def _feedback(
    client: httpx.AsyncClient, *, agent_id: UUID, token: str | None
) -> httpx.Response:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return await client.get(
        f"/api/v1/me/agents/{agent_id}/screening-feedback", headers=headers
    )


async def _mcp_feedback(
    client: httpx.AsyncClient, *, agent_id: UUID, token: str
) -> httpx.Response:
    return await client.post(
        "/mcp",
        headers={"authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "get_my_screening_feedback",
                "arguments": {"agent_id": str(agent_id)},
            },
        },
    )


def _assert_no_private_evidence(text: str) -> None:
    for value in _PRIVATE_EVIDENCE:
        assert value not in text, value


async def _owner(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> tuple[UUID, str]:
    miner = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = await _seed_agent(
        maker, status=AgentStatus.QUARANTINED, miner_hotkey=miner.ss58_address
    )
    _install_db(app, maker)
    _install_chain(app)
    return agent_id, await _login(client, keypair=miner)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "effective", "outcome", "next_step"),
    [
        ("reject", "reject", "rejected", "resubmit_after_fix"),
        ("clear", "pass", "cleared", "none"),
        # Shadow posture or a held v13 clear: what took effect is the hold.
        ("clear", "hold", "held_for_operator_review", "await_operator_review"),
        ("reject", "hold", "held_for_operator_review", "await_operator_review"),
        ("escalate", "hold", "held_for_operator_review", "await_operator_review"),
    ],
)
async def test_owner_gets_only_a_bounded_outcome_and_next_step(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    decision: str,
    effective: str,
    outcome: str,
    next_step: str,
) -> None:
    agent_id, token = await _owner(app, client, session_maker)
    attempt_id = await _seed_review(
        session_maker, agent_id=agent_id, decision=decision, effective=effective
    )

    response = await _feedback(client, agent_id=agent_id, token=token)

    assert response.status_code == 200, response.text
    attempt = response.json()["attempts"][0]
    assert attempt["attempt_id"] == str(attempt_id)
    assert attempt["review_outcome"] == {"outcome": outcome, "next_step": next_step}


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["clear", "reject", "escalate"])
async def test_private_review_evidence_stays_absent_after_verified_digests(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    decision: str,
) -> None:
    """Ledger and court digests both verify, and still nothing private leaks."""
    agent_id, token = await _owner(app, client, session_maker)
    await _seed_review(session_maker, agent_id=agent_id, decision=decision)

    http = await _feedback(client, agent_id=agent_id, token=token)
    mcp = await _mcp_feedback(client, agent_id=agent_id, token=token)

    assert http.status_code == 200, http.text
    assert mcp.status_code == 200, mcp.text
    result = mcp.json()["result"]
    assert result["isError"] is False
    mcp_text = result["content"][0]["text"]
    # Verified: the bounded outcome is present on both transports.
    assert http.json()["attempts"][0]["review_outcome"] is not None
    assert json.loads(mcp_text)["attempts"][0]["review_outcome"] is not None
    # The only review-derived keys on the attempt are the two enums.
    attempt = http.json()["attempts"][0]
    assert set(attempt["review_outcome"]) == {"outcome", "next_step"}
    assert attempt["public_reason"] == _PUBLIC_REASON
    _assert_no_private_evidence(http.text)
    _assert_no_private_evidence(mcp_text)


@pytest.mark.asyncio
async def test_unverified_or_ignored_court_decision_has_no_outcome(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    miner = bittensor.Keypair.create_from_uri("//Alice")
    _install_db(app, session_maker)
    _install_chain(app)
    token = await _login(client, keypair=miner)
    cases: list[dict[str, object]] = [
        {"decision": "reject", "adjudication_digest": "1" * 64},  # tampered
        {"decision": "clear", "effective": "no_change"},  # late result, ignored
        {"decision": "clear", "effective": "provisional_admission"},  # deferred review
        {"decision": "clear", "event": False},  # no automated event at all
        {"decision": "clear", "effective": "reject"},  # contradictory event
        {"decision": "reject", "effective": "pass"},  # contradictory event
        {"decision": "clear", "event_policy_version": 14},  # wrong snapshot
        {"decision": "clear", "event_artifact_sha256": "a" * 64},  # wrong artifact
        {"decision": "clear", "attempt_artifact_sha256": None},  # legacy unpinned
    ]
    for case in cases:
        # One active quarantine per agent, so each case gets its own agent.
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.QUARANTINED,
            miner_hotkey=miner.ss58_address,
        )
        attempt_id = await _seed_review(
            session_maker,
            agent_id=agent_id,
            decision=str(case["decision"]),
            effective=str(case.get("effective", "hold")),
            event=bool(case.get("event", True)),
            adjudication_digest=(
                str(case["adjudication_digest"])
                if "adjudication_digest" in case
                else None
            ),
            attempt_artifact_sha256=case.get("attempt_artifact_sha256", _SHA256),
            event_artifact_sha256=str(case.get("event_artifact_sha256", _SHA256)),
            event_policy_version=int(case.get("event_policy_version", 13)),
        )

        response = await _feedback(client, agent_id=agent_id, token=token)

        assert response.status_code == 200, response.text
        attempt = response.json()["attempts"][0]
        assert attempt["attempt_id"] == str(attempt_id)
        assert attempt["review_outcome"] is None, case
        _assert_no_private_evidence(response.text)


@pytest.mark.asyncio
async def test_other_miner_and_anonymous_caller_cannot_read_the_outcome(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    owner = bittensor.Keypair.create_from_uri("//Alice")
    attacker = bittensor.Keypair.create_from_uri("//Bob")
    agent_id = await _seed_agent(
        session_maker, status=AgentStatus.QUARANTINED, miner_hotkey=owner.ss58_address
    )
    await _seed_review(
        session_maker, agent_id=agent_id, decision="reject", effective="reject"
    )
    _install_db(app, session_maker)
    _install_chain(app)
    attacker_token = await _login(client, keypair=attacker)

    foreign = await _feedback(client, agent_id=agent_id, token=attacker_token)
    anonymous = await _feedback(client, agent_id=agent_id, token=None)
    foreign_mcp = await _mcp_feedback(client, agent_id=agent_id, token=attacker_token)
    public = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")

    assert foreign.status_code == 404
    assert anonymous.status_code == 401
    assert "review_outcome" not in foreign_mcp.text
    assert "resubmit_after_fix" not in foreign_mcp.text
    assert public.status_code == 200, public.text
    for response in (foreign, anonymous, foreign_mcp, public):
        assert "Routes a benchmark-shaped prompt" not in response.text
        assert _REJECT_REASON not in response.text
        assert "src/router.rs" not in response.text
