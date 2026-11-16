"""Three-lane single use of payment proofs (SN-485).

One finalized transfer funds exactly one thing across the agent-upload,
generator-upload and readmission lanes. Every test runs on the real Postgres
harness, so the advisory lock, primary keys, the partial unique index and the
check constraints are the real ones.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_server.payment_verifier import PaymentReplayedError, VerifiedPayment
from ditto.db.models import (
    EvaluationPayment,
    GeneratorSubmission,
    ReadmissionPayment,
)
from ditto.db.queries.agents import insert_agent
from ditto.db.queries.payments import (
    READMISSION_CREDIT_TTL,
    ReadmissionAlreadyAdmittedError,
    consume_evaluation_credit,
    credit_expires_at,
    get_evaluation_payment_for_proof,
    get_readmission_payment_for_proof,
    insert_evaluation_payment,
    insert_generator_payment,
    insert_readmission_payment,
    spend_credit_on_readmission,
)

pytestmark = pytest.mark.asyncio

HOTKEY = "5HKReadmitHotkey"
COLDKEY = "5CKReadmitColdkey"


def _verified(salt: int, **overrides: Any) -> VerifiedPayment:
    base: dict[str, Any] = {
        "block_hash": f"0x{salt:064x}",
        "extrinsic_index": 2,
        "miner_hotkey": HOTKEY,
        "miner_coldkey": COLDKEY,
        "amount_rao": 75_000_000,
        "tao_usd_rate": Decimal("400"),
        "dest_address": "5DestAddress",
        "block_timestamp": datetime.now(UTC) - timedelta(minutes=5),
    }
    base.update(overrides)
    return VerifiedPayment(**base)


async def _agent(session: AsyncSession, *, hotkey: str = HOTKEY) -> UUID:
    agent_id = uuid4()
    async with session.begin():
        await insert_agent(
            session,
            agent_id=agent_id,
            miner_hotkey=hotkey,
            name=f"readmit-{agent_id.hex[:8]}",
            sha256=uuid4().hex + uuid4().hex,
            size_bytes=1024,
        )
    return agent_id


async def _generator_submission(session: AsyncSession) -> UUID:
    submission_id = uuid4()
    source = b"package main\n"
    async with session.begin():
        session.add(
            GeneratorSubmission(
                submission_id=submission_id,
                miner_hotkey=HOTKEY,
                owner_key=f"coldkey:{COLDKEY}",
                source_sha256=uuid4().hex + uuid4().hex,
                tree_sha256=uuid4().hex + uuid4().hex,
                source_bytes=source,
                size_bytes=len(source),
                module="example.com/gen",
                go_version="1.22",
                file_count=1,
                status="active",
                next_run_at=datetime.now(UTC),
                submitted_at=datetime.now(UTC),
            )
        )
    return submission_id


async def test_an_upload_proof_never_pays_a_readmission_or_a_generator(
    session: AsyncSession,
) -> None:
    uploaded = await _agent(session)
    target = await _agent(session)
    submission = await _generator_submission(session)
    proof = _verified(1)
    async with session.begin():
        await insert_evaluation_payment(session, verified=proof, agent_id=uploaded)

    with pytest.raises(PaymentReplayedError):
        async with session.begin():
            await insert_readmission_payment(
                session,
                verified=proof,
                agent_id=target,
                bench_version=19,
                rollout_id=None,
            )
    with pytest.raises(PaymentReplayedError):
        async with session.begin():
            await insert_generator_payment(
                session, verified=proof, submission_id=submission
            )


async def test_a_generator_proof_never_pays_a_readmission(
    session: AsyncSession,
) -> None:
    target = await _agent(session)
    submission = await _generator_submission(session)
    proof = _verified(2)
    async with session.begin():
        await insert_generator_payment(
            session, verified=proof, submission_id=submission
        )
    with pytest.raises(PaymentReplayedError):
        async with session.begin():
            await insert_readmission_payment(
                session,
                verified=proof,
                agent_id=target,
                bench_version=19,
                rollout_id=None,
            )


async def test_a_readmission_proof_never_funds_an_upload_or_a_generator(
    session: AsyncSession,
) -> None:
    target = await _agent(session)
    other = await _agent(session)
    submission = await _generator_submission(session)
    proof = _verified(3)
    async with session.begin():
        row = await insert_readmission_payment(
            session, verified=proof, agent_id=target, bench_version=19, rollout_id=None
        )
        assert row.outcome == "admitted" and row.funding == "fresh"

    for destination in ({"agent_id": other}, {"credit_for_agent_id": other}):
        with pytest.raises(PaymentReplayedError, match="readmission"):
            async with session.begin():
                await insert_evaluation_payment(
                    session,
                    verified=proof,
                    **destination,  # type: ignore[arg-type]
                )
    with pytest.raises(PaymentReplayedError):
        async with session.begin():
            await insert_generator_payment(
                session, verified=proof, submission_id=submission
            )
    with pytest.raises(PaymentReplayedError):
        async with session.begin():
            await insert_readmission_payment(
                session,
                verified=proof,
                agent_id=other,
                bench_version=19,
                rollout_id=None,
            )


async def test_at_most_one_admitted_readmission_per_agent_and_era(
    session: AsyncSession,
) -> None:
    target = await _agent(session)
    async with session.begin():
        await insert_readmission_payment(
            session,
            verified=_verified(4),
            agent_id=target,
            bench_version=19,
            rollout_id=None,
        )
    with pytest.raises(ReadmissionAlreadyAdmittedError):
        async with session.begin():
            await insert_readmission_payment(
                session,
                verified=_verified(5),
                agent_id=target,
                bench_version=19,
                rollout_id=None,
            )
    # The next era is a separate readmission.
    async with session.begin():
        await insert_readmission_payment(
            session,
            verified=_verified(6),
            agent_id=target,
            bench_version=20,
            rollout_id=None,
        )


async def test_a_refused_readmission_banks_a_30_day_upload_credit(
    session: AsyncSession,
) -> None:
    target = await _agent(session)
    next_upload = await _agent(session)
    submission = await _generator_submission(session)
    proof = _verified(7, block_timestamp=datetime.now(UTC) - timedelta(days=3))
    async with session.begin():
        await insert_readmission_payment(
            session,
            verified=proof,
            agent_id=target,
            bench_version=19,
            rollout_id=None,
            outcome="credited",
            refusal_reason="a benchmark rollout opened after payment",
        )
    credit = await get_evaluation_payment_for_proof(
        session, block_hash=proof.block_hash, extrinsic_index=proof.extrinsic_index
    )
    assert credit is not None
    assert credit.agent_id is None and credit.credit_for_agent_id == target
    assert credit.credit_origin == "readmission"
    # Three days old and still spendable: the readmission window is 30 days.
    assert credit_expires_at(credit) == proof.block_timestamp + READMISSION_CREDIT_TTL
    await session.commit()

    # The generator lane cannot spend it.
    with pytest.raises(PaymentReplayedError):
        async with session.begin():
            await insert_generator_payment(
                session, verified=proof, submission_id=submission
            )

    # The miner's next upload spends it like any credit; the readmission row
    # stays as the accounting record.
    async with session.begin():
        locked = await get_evaluation_payment_for_proof(
            session,
            block_hash=proof.block_hash,
            extrinsic_index=proof.extrinsic_index,
            for_update=True,
        )
        assert locked is not None
        await consume_evaluation_credit(
            session, payment=locked, agent_id=next_upload, miner_hotkey=HOTKEY
        )
    readmission = await get_readmission_payment_for_proof(
        session, block_hash=proof.block_hash, extrinsic_index=proof.extrinsic_index
    )
    assert readmission is not None and readmission.outcome == "credited"
    await session.commit()

    # Once an upload spent it, a readmission cannot.
    async with session.begin():
        spent = await get_evaluation_payment_for_proof(
            session,
            block_hash=proof.block_hash,
            extrinsic_index=proof.extrinsic_index,
            for_update=True,
        )
        assert spent is not None
        with pytest.raises(PaymentReplayedError):
            await spend_credit_on_readmission(
                session,
                credit=spent,
                agent_id=target,
                bench_version=19,
                rollout_id=None,
                miner_hotkey=HOTKEY,
            )


async def test_a_credited_readmission_can_be_spent_on_the_readmission(
    session: AsyncSession,
) -> None:
    target = await _agent(session)
    other = await _agent(session)
    proof = _verified(8)
    async with session.begin():
        await insert_readmission_payment(
            session,
            verified=proof,
            agent_id=target,
            bench_version=19,
            rollout_id=None,
            outcome="credited",
            refusal_reason="the cohort filled after payment",
        )
    async with session.begin():
        credit = await get_evaluation_payment_for_proof(
            session,
            block_hash=proof.block_hash,
            extrinsic_index=proof.extrinsic_index,
            for_update=True,
        )
        assert credit is not None
        row = await spend_credit_on_readmission(
            session,
            credit=credit,
            agent_id=target,
            bench_version=19,
            rollout_id=None,
            miner_hotkey=HOTKEY,
        )
        assert (row.outcome, row.funding, row.refusal_reason) == (
            "admitted",
            "credit",
            None,
        )
        # The spent credit's history survives on the readmission row.
        assert row.credit_audit is not None
        assert row.credit_audit["credit_origin"] == "readmission"
        assert row.credit_audit["credit_for_agent_id"] == str(target)
        assert row.credit_audit["refusal_reason"] == "the cohort filled after payment"
    # The credit is gone, so no upload can spend the proof again.
    assert (
        await get_evaluation_payment_for_proof(
            session, block_hash=proof.block_hash, extrinsic_index=proof.extrinsic_index
        )
        is None
    )
    await session.commit()
    with pytest.raises(PaymentReplayedError):
        async with session.begin():
            await insert_evaluation_payment(session, verified=proof, agent_id=other)


async def test_a_duplicate_upload_credit_can_pay_a_readmission_once(
    session: AsyncSession,
) -> None:
    original = await _agent(session)
    target = await _agent(session)
    proof = _verified(9)
    async with session.begin():
        await insert_evaluation_payment(
            session, verified=proof, credit_for_agent_id=original
        )
    async with session.begin():
        credit = await get_evaluation_payment_for_proof(
            session,
            block_hash=proof.block_hash,
            extrinsic_index=proof.extrinsic_index,
            for_update=True,
        )
        assert credit is not None
        await spend_credit_on_readmission(
            session,
            credit=credit,
            agent_id=target,
            bench_version=19,
            rollout_id=None,
            miner_hotkey=HOTKEY,
        )
    rows = list(
        await session.scalars(
            select(ReadmissionPayment).where(ReadmissionPayment.agent_id == target)
        )
    )
    assert [(r.outcome, r.funding) for r in rows] == [("admitted", "credit")]
    assert rows[0].credit_audit is not None
    assert rows[0].credit_audit["credit_origin"] == "duplicate_upload"
    assert rows[0].credit_audit["credit_for_agent_id"] == str(original)
    assert (
        await session.scalar(
            select(EvaluationPayment).where(
                EvaluationPayment.block_hash == proof.block_hash
            )
        )
        is None
    )


@pytest.mark.parametrize("problem", ["other_hotkey", "expired"])
async def test_a_credit_is_refused_for_another_hotkey_or_after_its_window(
    session: AsyncSession, problem: str
) -> None:
    original = await _agent(session)
    target = await _agent(session)
    paid_at = datetime.now(UTC) - (
        timedelta(hours=25) if problem == "expired" else timedelta(minutes=1)
    )
    proof = _verified(10 if problem == "expired" else 11, block_timestamp=paid_at)
    async with session.begin():
        await insert_evaluation_payment(
            session, verified=proof, credit_for_agent_id=original
        )
    async with session.begin():
        credit = await get_evaluation_payment_for_proof(
            session,
            block_hash=proof.block_hash,
            extrinsic_index=proof.extrinsic_index,
            for_update=True,
        )
        assert credit is not None
        with pytest.raises(PaymentReplayedError):
            await spend_credit_on_readmission(
                session,
                credit=credit,
                agent_id=target,
                bench_version=19,
                rollout_id=None,
                miner_hotkey="5OtherHotkey" if problem == "other_hotkey" else HOTKEY,
            )


async def test_credited_needs_a_reason_and_admitted_refuses_one(
    session: AsyncSession,
) -> None:
    target = await _agent(session)
    with pytest.raises(ValueError):
        await insert_readmission_payment(
            session,
            verified=_verified(12),
            agent_id=target,
            bench_version=19,
            rollout_id=None,
            outcome="credited",
        )
    with pytest.raises(ValueError):
        await insert_readmission_payment(
            session,
            verified=_verified(12),
            agent_id=target,
            bench_version=19,
            rollout_id=None,
            refusal_reason="not a refusal",
        )


async def test_a_whitespace_reason_on_an_admitted_readmission_is_ignored(
    session: AsyncSession,
) -> None:
    target = await _agent(session)
    async with session.begin():
        row = await insert_readmission_payment(
            session,
            verified=_verified(13),
            agent_id=target,
            bench_version=19,
            rollout_id=None,
            refusal_reason="   ",
        )
        assert row.outcome == "admitted" and row.refusal_reason is None
