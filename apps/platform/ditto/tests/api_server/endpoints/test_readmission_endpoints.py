"""Signed self-serve readmission: check, reserve, pay, commit (SN-485).

Runs on the real Postgres harness against an activated v13 -> v14 era. The gate's
first charged era is lowered to v14 for the test (production pins v19), and an
announcement eight days old is written straight to the settings table.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.readmission import readmission_signature_payload
from ditto.api_server.dependencies import get_payment_verifier, get_session
from ditto.api_server.payment_verifier import VerifiedPayment
from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    BenchmarkRolloutAudit,
    BenchmarkRolloutMember,
    EvaluationPayment,
    ReadmissionPayment,
    ReadmissionReservation,
    Score,
    SubmissionSettingsRevision,
)
from ditto.db.queries import readmission_gate
from ditto.db.queries.benchmark_admission import agent_is_admitted
from ditto.db.queries.stranded_admission import EVENT_STRANDED_ADMITTED
from ditto.tests.api_server.conftest import override_get_chain_client
from ditto_screening_protocol import SCREENING_FLOOR_POLICY_VERSION

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("wired")]

_FROM = 13
_ACTIVE = 14
_COLDKEY = "5Coldkey"
_FEE = 75_000_000
_ROLLOUT_START = datetime(2026, 10, 28, 0, 30, tzinfo=UTC)
_BEFORE = _ROLLOUT_START - timedelta(days=1)
_KEYPAIR = bittensor.Keypair.create_from_uri("//Alice")
_HOTKEY = _KEYPAIR.ss58_address


class _StubGenerator:
    run_size = "full"

    async def generate(self, seed: int, bench_version: int = 2) -> str:
        del bench_version
        return f"{seed:064x}"

    async def aclose(self) -> None:
        return None


@pytest.fixture
def wired(
    app: FastAPI,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> MagicMock:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.state.dataset_generator = _StubGenerator()
    override_get_chain_client(app)
    verifier = MagicMock()
    verifier.verify_payment = AsyncMock(side_effect=lambda proof, **_: _verified(proof))

    async def _verifier() -> MagicMock:
        return verifier

    app.dependency_overrides[get_payment_verifier] = _verifier
    monkeypatch.setattr(readmission_gate, "READMISSION_MIN_BENCH_VERSION", _ACTIVE)
    return verifier


def _verified(proof: Any) -> VerifiedPayment:
    return VerifiedPayment(
        block_hash=proof.block_hash,
        extrinsic_index=proof.extrinsic_index,
        miner_hotkey=_HOTKEY,
        miner_coldkey=_COLDKEY,
        amount_rao=_FEE,
        tao_usd_rate=Decimal("400"),
        dest_address="5Dest",
        block_timestamp=datetime.now(UTC) - timedelta(minutes=1),
    )


def _sha(name: str) -> str:
    return f"{abs(hash(name)) % (16**64):064x}"


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    *,
    announced: bool = True,
    previous_scores: int = 3,
) -> UUID:
    """An activated v13 -> v14 era and one stranded agent paid by ``_COLDKEY``."""
    async with maker() as session, session.begin():
        latest = await session.scalar(
            select(SubmissionSettingsRevision)
            .order_by(SubmissionSettingsRevision.revision.desc())
            .limit(1)
        )
        session.add(
            SubmissionSettingsRevision(
                parent_revision=latest.revision if latest else 0,
                cooldown_seconds=3600,
                fee_amount_rao=_FEE,
                competition_fees={},
                readmission_announced_at=(
                    datetime.now(UTC) - timedelta(days=8) if announced else None
                ),
                reason="test readmission announcement",
                actor="test",
            )
        )
        rollout = BenchmarkRollout(
            rollout_id=uuid4(),
            from_version=_FROM,
            desired_version=_ACTIVE,
            status="activated",
            cohort_size=5,
            created_at=_ROLLOUT_START,
            activated_at=_ROLLOUT_START + timedelta(hours=1),
            rescore_cohort_target=5,
            priority_cohort_target=5,
        )
        session.add(rollout)
        agent_id = uuid4()
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=_HOTKEY,
                name="stranded",
                version=1,
                sha256=_sha(str(agent_id)),
                status=AgentStatus.SCORED,
                screening_policy_version=SCREENING_FLOOR_POLICY_VERSION,
                created_at=_BEFORE,
                screened_image_sha256="e" * 64,
                screened_image_size_bytes=1024,
                screened_image_id="sha256:" + "e" * 64,
                screened_image_ref=f"ditto-screen/{agent_id}:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=_BEFORE,
            )
        )
        await session.flush()
        session.add(
            EvaluationPayment(
                block_hash=f"0x{agent_id.int:064x}",
                extrinsic_index=0,
                agent_id=agent_id,
                miner_hotkey=_HOTKEY,
                miner_coldkey=_COLDKEY,
                amount_rao=_FEE,
                dest_address="5Dest",
                timestamp=_BEFORE,
            )
        )
        for index in range(previous_scores):
            session.add(
                Score(
                    agent_id=agent_id,
                    bench_version=_FROM,
                    validator_hotkey=f"5Validator-{index}",
                    run_id=f"{agent_id}-{index}",
                    signature=None,
                    seed=7 + index,
                    composite=0.8,
                    tool_mean=0.8,
                    memory_mean=0.8,
                    median_ms=1,
                    n=350,
                    details={"bench_version": _FROM},
                    generated_at=_BEFORE,
                )
            )
        return agent_id


def _signed(
    agent_id: UUID,
    *,
    bench_version: int = _ACTIVE,
    upload_domain: bool = False,
) -> dict[str, Any]:
    timestamp = int(time.time())
    nonce = uuid4()
    payload = (
        f"ditto-upload-v2:{_HOTKEY}:{agent_id}:{timestamp}:{nonce}".encode("ascii")
        if upload_domain
        else readmission_signature_payload(
            hotkey=_HOTKEY,
            agent_id=agent_id,
            bench_version=bench_version,
            signature_timestamp=timestamp,
            signature_nonce=nonce,
        )
    )
    return {
        "hotkey": _HOTKEY,
        "agent_id": str(agent_id),
        "bench_version": bench_version,
        "signature": _KEYPAIR.sign(payload).hex(),
        "signature_timestamp": timestamp,
        "signature_nonce": str(nonce),
    }


def _proof(salt: int) -> dict[str, Any]:
    return {
        "payment_block_hash": f"0x{salt:064x}",
        "payment_block_number": 1000 + salt,
        "payment_extrinsic_index": 1,
    }


async def _check(client: httpx.AsyncClient, agent_id: UUID, **extra: Any) -> Any:
    resp = await client.post(
        "/api/v1/readmission/check", json={**_signed(agent_id), **extra}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_unannounced_readmission_is_refused_before_payment(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(session_maker, announced=False)
    body = await _check(client, agent_id, reserve_slot=True)
    assert body["ok"] is False
    assert 1110 in body["error_codes"]
    assert body["activation"]["inactive_reasons"] == ["announcement_unset"]
    assert body["admission_token"] is None
    assert body["payment_amount_rao"] is None
    # Eligibility is still reported, so the miner sees what would happen.
    assert body["eligibility"]["stranded"] is True


async def test_paid_readmission_admits_audits_and_starts_the_cooldown(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    wired: MagicMock,
) -> None:
    agent_id = await _seed(session_maker)
    body = await _check(client, agent_id, reserve_slot=True)
    assert body["ok"] is True, body["messages"]
    assert body["activation"]["active"] is True
    assert body["payment_amount_rao"] == _FEE
    assert body["eligibility"]["owner_best_stranded"] is True
    assert body["eligibility"]["cohort_sibling_agent_id"] is None
    assert body["eligibility"]["previous_score_count"] == 3
    token = body["admission_token"]
    assert token is not None

    resp = await client.post(
        "/api/v1/readmission/commit",
        json={**_signed(agent_id), "admission_token": token, **_proof(1)},
    )
    assert resp.status_code == 200, resp.text
    committed = resp.json()
    assert committed["outcome"] == "admitted"
    assert committed["payment_disposition"] == "consumed"
    assert committed["bench_version"] == _ACTIVE
    wired.verify_payment.assert_awaited_once()
    assert wired.verify_payment.await_args.kwargs["expected_amount_rao"] == _FEE

    async with session_maker() as session:
        assert await agent_is_admitted(
            session, bench_version=_ACTIVE, agent_id=agent_id
        )
        member = await session.scalar(
            select(BenchmarkRolloutMember).where(
                BenchmarkRolloutMember.agent_id == agent_id
            )
        )
        assert member is not None
        audit = await session.scalar(
            select(BenchmarkRolloutAudit).where(
                BenchmarkRolloutAudit.event == EVENT_STRANDED_ADMITTED
            )
        )
        assert audit is not None
        assert audit.payload["origin"] == "paid_readmission"
        assert audit.payload["category"] == "paid"
        assert audit.payload["payment_ref"]["amount_rao"] == _FEE
        paid = list(await session.scalars(select(ReadmissionPayment)))
        assert [(row.outcome, row.agent_id) for row in paid] == [("admitted", agent_id)]
        assert await session.scalar(select(ReadmissionReservation)) is None

    # An exact retry returns the original admission.
    retry = await client.post(
        "/api/v1/readmission/commit",
        json={**_signed(agent_id), "admission_token": token, **_proof(1)},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["outcome"] == "admitted"
    assert retry.json()["recovered"] is True

    # Now admitted, and the readmission started the owner's cooldown.
    again = await _check(client, agent_id)
    assert 1112 in again["error_codes"]
    assert 1105 in again["error_codes"]
    assert again["retry_at"] is not None


async def test_a_refusal_found_after_payment_banks_a_30_day_credit(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(session_maker)
    token = (await _check(client, agent_id, reserve_slot=True))["admission_token"]
    # The era moves on under the miner after they paid: the agent now holds an
    # active-version score, so it is no longer stranded.
    async with session_maker() as session, session.begin():
        session.add(
            Score(
                agent_id=agent_id,
                bench_version=_ACTIVE,
                validator_hotkey="5Validator-active",
                run_id=f"{agent_id}-active",
                signature=None,
                seed=99,
                composite=0.7,
                tool_mean=0.7,
                memory_mean=0.7,
                median_ms=1,
                n=350,
                details={"bench_version": _ACTIVE},
                generated_at=datetime.now(UTC),
            )
        )
    resp = await client.post(
        "/api/v1/readmission/commit",
        json={**_signed(agent_id), "admission_token": token, **_proof(2)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "credited"
    assert body["payment_disposition"] == "credited"
    assert "not a scored previous-era agent" in body["refusal_reason"]
    expires = datetime.fromisoformat(body["credit_expires_at"])
    assert expires > datetime.now(UTC) + timedelta(days=29)
    async with session_maker() as session:
        row = await session.scalar(select(ReadmissionPayment))
        assert row is not None and row.outcome == "credited"
        credit = await session.scalar(
            select(EvaluationPayment).where(
                EvaluationPayment.credit_origin == "readmission"
            )
        )
        assert credit is not None
        assert credit.agent_id is None and credit.credit_for_agent_id == agent_id


@pytest.mark.parametrize("problem", ["partial", "other_owner", "wrong_era"])
async def test_ineligible_rows_are_refused_before_payment(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    problem: str,
) -> None:
    agent_id = await _seed(
        session_maker, previous_scores=2 if problem == "partial" else 3
    )
    if problem == "other_owner":
        from ditto.api_server.dependencies import get_chain_client
        from ditto.tests.api_server.conftest import fake_registration

        chain = MagicMock()
        chain.get_hotkey_registration = AsyncMock(
            side_effect=lambda hotkey, netuid, **_: fake_registration(
                hotkey, netuid, "5NewOwnerColdkey"
            )
        )

        async def _chain() -> MagicMock:
            return chain

        app.dependency_overrides[get_chain_client] = _chain
    version = _FROM if problem == "wrong_era" else _ACTIVE
    resp = await client.post(
        "/api/v1/readmission/check",
        json={**_signed(agent_id, bench_version=version), "reserve_slot": True},
    )
    body = resp.json()
    assert body["ok"] is False
    expected = {"partial": 1111, "other_owner": 1113, "wrong_era": 1111}[problem]
    assert expected in body["error_codes"], body["messages"]
    assert body["admission_token"] is None


async def test_an_upload_signature_cannot_stand_in_for_a_readmission(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    wired: MagicMock,
) -> None:
    agent_id = await _seed(session_maker)
    forged = _signed(agent_id, upload_domain=True)
    body = (await client.post("/api/v1/readmission/check", json=forged)).json()
    assert 1100 in body["error_codes"]
    resp = await client.post("/api/v1/readmission/commit", json={**forged, **_proof(3)})
    assert resp.status_code == 400
    wired.verify_payment.assert_not_awaited()


async def test_a_recent_upload_holds_the_readmission_cooldown(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(session_maker)
    async with session_maker() as session, session.begin():
        recent = uuid4()
        session.add(
            Agent(
                agent_id=recent,
                miner_hotkey=_HOTKEY,
                name="recent",
                version=2,
                sha256=_sha(str(recent)),
                status=AgentStatus.UPLOADED,
                screening_policy_version=SCREENING_FLOOR_POLICY_VERSION,
                created_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        await session.flush()
        session.add(
            EvaluationPayment(
                block_hash=f"0x{recent.int:064x}",
                extrinsic_index=0,
                agent_id=recent,
                miner_hotkey=_HOTKEY,
                miner_coldkey=_COLDKEY,
                amount_rao=_FEE,
                dest_address="5Dest",
                timestamp=datetime.now(UTC),
            )
        )
    body = await _check(client, agent_id, reserve_slot=True)
    assert body["ok"] is False
    assert 1105 in body["error_codes"]
    assert body["admission_token"] is None


async def test_an_agent_retired_from_the_previous_era_is_refused(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from ditto.db.models import SubmissionRetirement

    agent_id = await _seed(session_maker)
    async with session_maker() as session, session.begin():
        session.add(
            SubmissionRetirement(
                retirement_id=uuid4(),
                agent_id=agent_id,
                bench_version=_FROM,
                superseded_by_version=_ACTIVE,
                actor="operator@example.test",
                reason="retired from the previous era's queue",
                expected_snapshot="0" * 64,
                score_count=3,
                ticket_snapshot=[],
            )
        )
    body = await _check(client, agent_id, reserve_slot=True)
    assert body["ok"] is False
    assert 1111 in body["error_codes"]
    assert any(f"retired from the v{_FROM}" in m for m in body["messages"])
    assert body["admission_token"] is None


async def test_the_operator_lookup_counts_a_readmission_paid_by_a_linked_coldkey(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from ditto.db.queries.owner_submission_cooldown import (
        identity_latest_submission_at,
    )

    agent_id = await _seed(session_maker)
    token = (await _check(client, agent_id, reserve_slot=True))["admission_token"]
    resp = await client.post(
        "/api/v1/readmission/commit",
        json={**_signed(agent_id), "admission_token": token, **_proof(9)},
    )
    assert resp.json()["outcome"] == "admitted", resp.text
    now = datetime.now(UTC)
    async with session_maker() as session:
        by_hotkey = await identity_latest_submission_at(
            session, scope="hotkey", owner_key=_HOTKEY, now=now
        )
        by_coldkey = await identity_latest_submission_at(
            session, scope="coldkey", owner_key=_COLDKEY, now=now
        )
    # The readmission (just now) is newer than the agent's upload (_BEFORE).
    assert by_hotkey is not None and by_hotkey > now - timedelta(minutes=5)
    assert by_coldkey is not None and by_coldkey > now - timedelta(minutes=5)
