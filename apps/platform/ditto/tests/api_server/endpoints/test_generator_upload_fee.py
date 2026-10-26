"""The optional TAO fee on generator uploads (``upload_fee_required``).

Off by default: uploads behave exactly as before and never touch the payment
verifier. On: an upload must carry a finalized payment proof, verified by the
agent-upload verifier against the agent-upload fee and address, and a proof
funds exactly one upload across the agent and generator lanes.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.generator import GeneratorCompetitionSettings
from ditto.api_server.endpoints.generator import FEE_REQUIRED_DETAIL
from ditto.api_server.generator_competition import settings_from_row
from ditto.api_server.payment_verifier import (
    PaymentAmountMismatch,
    PaymentProof,
    PaymentReplayedError,
    VerifiedPayment,
)
from ditto.db.models import (
    EvaluationPayment,
    GeneratorSubmission,
    GeneratorSubmissionPayment,
)
from ditto.db.queries.agents import insert_agent
from ditto.db.queries.payments import insert_evaluation_payment
from ditto.db.queries.submission_settings import effective_submission_settings
from ditto.tests.api_server.endpoints.test_generator_competition import (
    _MINER,
    _MINER_COLDKEY,
    _OTHER_MINER,
    _archive,
    _install,
    _open_lane,
    _settings,
    _skip_cooldown,
    _upload_body,
)

_UPLOAD_URL = "/api/v1/generator/submissions"
_TERMS_URL = "/api/v1/generator/upload-terms"


def _block_hash(salt: int) -> str:
    return "0x" + format(salt, "064x")


def _with_proof(
    body: dict[str, Any], block_hash: str, *, extrinsic_index: int = 1
) -> dict[str, Any]:
    return {
        **body,
        "payment_block_hash": block_hash,
        "payment_block_number": 4242,
        "payment_extrinsic_index": extrinsic_index,
    }


def _fake_verifier(
    *, paid_at: datetime | None = None, error: Exception | None = None
) -> MagicMock:
    """Accepts any proof as a finalized transfer of exactly the expected fee."""

    async def _verify(
        proof: PaymentProof,
        expected_hotkey: str,
        expected_amount_rao: int,
        legacy_amount_cutoff_at: datetime | None = None,  # noqa: ARG001
        expected_send_address: str | None = None,
    ) -> VerifiedPayment:
        if error is not None:
            raise error
        assert expected_send_address is not None
        return VerifiedPayment(
            block_hash=proof.block_hash,
            extrinsic_index=proof.extrinsic_index,
            miner_hotkey=expected_hotkey,
            miner_coldkey=_MINER_COLDKEY,
            amount_rao=expected_amount_rao,
            tao_usd_rate=Decimal("400"),
            dest_address=expected_send_address,
            block_timestamp=paid_at or datetime.now(UTC),
        )

    verifier = MagicMock()
    verifier.verify_payment = AsyncMock(side_effect=_verify)
    return verifier


async def _agent_fee(
    maker: async_sessionmaker[AsyncSession], app: FastAPI
) -> tuple[int, str]:
    """The fee and address an agent upload would be quoted right now."""
    async with maker() as session:
        fee = await effective_submission_settings(
            session,
            default_payment_address=app.state.config.upload_payment_address,
        )
    return fee.fee_amount_rao, fee.payment_address


async def _count(maker: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with maker() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


def test_a_revision_written_before_the_field_reads_as_fee_off() -> None:
    stored = _settings()
    stored.pop("upload_fee_required")
    row = MagicMock(settings=stored, revision=3)
    settings, valid = settings_from_row(row)
    assert valid
    assert settings.upload_fee_required is False
    assert GeneratorCompetitionSettings().upload_fee_required is False


class TestFeeOff:
    async def test_uploads_are_unchanged_and_never_verify(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        verifier = _fake_verifier()
        app.state.payment_verifier = verifier
        await _open_lane(client)

        terms = (await client.get(_TERMS_URL)).json()
        assert terms["accepting"] is True
        assert terms["payment_required"] is False
        assert terms["payment_amount_rao"] is None
        assert terms["payment_send_address"] is None

        resp = await client.post(_UPLOAD_URL, json=_upload_body(_MINER, _archive("a")))
        assert resp.status_code == 200, resp.text
        assert set(resp.json()) == {
            "submission_id",
            "source_sha256",
            "tree_sha256",
            "status",
            "superseded",
        }
        # A proof sent while the fee is off is ignored, not verified or spent.
        await _skip_cooldown(session_maker)
        resp = await client.post(
            _UPLOAD_URL,
            json=_with_proof(_upload_body(_MINER, _archive("b")), _block_hash(1)),
        )
        assert resp.status_code == 200, resp.text
        verifier.verify_payment.assert_not_awaited()
        assert await _count(session_maker, GeneratorSubmissionPayment) == 0

    async def test_partial_proof_is_a_validation_error(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client)
        body = _upload_body(_MINER, _archive("p"))
        body["payment_block_hash"] = _block_hash(2)
        resp = await client.post(_UPLOAD_URL, json=body)
        assert resp.status_code == 422, resp.text


class TestFeeOn:
    async def test_terms_report_the_agent_fee_and_blockers(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        app.state.payment_verifier = _fake_verifier()
        await _open_lane(client, upload_fee_required=True)

        raw = _archive("terms")
        terms = (await client.get(_TERMS_URL)).json()
        assert terms["payment_required"] is True
        amount, address = await _agent_fee(session_maker, app)
        assert terms["payment_amount_rao"] == amount
        assert terms["payment_send_address"] == address
        assert terms["blockers"] == []

        resp = await client.post(
            _UPLOAD_URL, json=_with_proof(_upload_body(_MINER, raw), _block_hash(10))
        )
        assert resp.status_code == 200, resp.text
        terms = (
            await client.get(
                _TERMS_URL,
                params={
                    "hotkey": _MINER.ss58_address,
                    "source_sha256": hashlib.sha256(raw).hexdigest(),
                },
            )
        ).json()
        assert terms["retry_at"] is not None
        assert len(terms["blockers"]) == 2
        assert any("cooldown" in b for b in terms["blockers"])
        assert any("already uploaded" in b for b in terms["blockers"])

    async def test_missing_proof_is_refused_before_any_work(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        verifier = _fake_verifier()
        app.state.payment_verifier = verifier
        await _open_lane(client, upload_fee_required=True)

        resp = await client.post(_UPLOAD_URL, json=_upload_body(_MINER, _archive("x")))
        assert resp.status_code == 402, resp.text
        assert resp.json()["message"] == FEE_REQUIRED_DETAIL
        verifier.verify_payment.assert_not_awaited()
        assert await _count(session_maker, GeneratorSubmission) == 0

    async def test_valid_proof_is_accepted_recorded_and_retry_safe(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        verifier = _fake_verifier()
        app.state.payment_verifier = verifier
        await _open_lane(client, upload_fee_required=True)

        proof = _block_hash(0xABC)
        # Mixed-case hex is the same proof; it is stored canonical.
        body = _with_proof(
            _upload_body(_MINER, _archive("paid")), "0x" + proof[2:].upper()
        )
        resp = await client.post(_UPLOAD_URL, json=body)
        assert resp.status_code == 200, resp.text
        submission_id = UUID(resp.json()["submission_id"])
        kwargs = verifier.verify_payment.await_args.kwargs
        amount, address = await _agent_fee(session_maker, app)
        assert kwargs["expected_amount_rao"] == amount
        assert kwargs["expected_send_address"] == address
        async with session_maker() as session:
            payment = await session.scalar(select(GeneratorSubmissionPayment))
            assert payment is not None
            assert payment.block_hash == proof
            assert payment.submission_id == submission_id
            assert payment.miner_hotkey == _MINER.ss58_address
            assert payment.amount_rao == amount

        # A lost response: the same hotkey, archive and proof get the same
        # submission back without a second verification or a new row.
        retry = await client.post(_UPLOAD_URL, json=body)
        assert retry.status_code == 200, retry.text
        assert UUID(retry.json()["submission_id"]) == submission_id
        assert verifier.verify_payment.await_count == 1
        assert await _count(session_maker, GeneratorSubmission) == 1

        # A paid upload that superseded an earlier generator reports the same
        # superseded list on an exact retry.
        await _skip_cooldown(session_maker)
        second = _with_proof(
            _upload_body(_MINER, _archive("paid-2")), _block_hash(0xABD)
        )
        accepted = await client.post(_UPLOAD_URL, json=second)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["superseded"] == [str(submission_id)]
        again = await client.post(_UPLOAD_URL, json=second)
        assert again.status_code == 200, again.text
        assert again.json() == accepted.json()

    async def test_a_spent_proof_cannot_fund_another_generator(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        app.state.payment_verifier = _fake_verifier()
        await _open_lane(client, upload_fee_required=True)

        proof = _block_hash(20)
        first = await client.post(
            _UPLOAD_URL, json=_with_proof(_upload_body(_MINER, _archive("one")), proof)
        )
        assert first.status_code == 200, first.text
        await _skip_cooldown(session_maker)
        for key, salt in ((_MINER, "two"), (_OTHER_MINER, "three")):
            resp = await client.post(
                _UPLOAD_URL, json=_with_proof(_upload_body(key, _archive(salt)), proof)
            )
            assert resp.status_code == 402, resp.text
            assert resp.json()["error_code"] == 3207
        assert await _count(session_maker, GeneratorSubmission) == 1

    async def test_agent_and_generator_lanes_never_share_a_proof(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        app.state.payment_verifier = _fake_verifier()
        await _open_lane(client, upload_fee_required=True)

        # A proof an agent upload spent cannot fund a generator.
        agent_proof = _block_hash(30)
        agent_id = uuid4()
        async with session_maker() as session, session.begin():
            await insert_agent(
                session,
                agent_id=agent_id,
                miner_hotkey=_MINER.ss58_address,
                name="fee-agent",
                sha256="ab" * 32,
                size_bytes=10,
            )
            await insert_evaluation_payment(
                session,
                verified=VerifiedPayment(
                    block_hash=agent_proof,
                    extrinsic_index=1,
                    miner_hotkey=_MINER.ss58_address,
                    miner_coldkey=_MINER_COLDKEY,
                    amount_rao=40_000_000,
                    tao_usd_rate=None,
                    dest_address=app.state.config.upload_payment_address,
                    block_timestamp=datetime.now(UTC),
                ),
                agent_id=agent_id,
            )
        resp = await client.post(
            _UPLOAD_URL,
            json=_with_proof(_upload_body(_MINER, _archive("g")), agent_proof),
        )
        assert resp.status_code == 402, resp.text
        assert resp.json()["error_code"] == 3207
        assert await _count(session_maker, GeneratorSubmission) == 0

        # A proof a generator upload spent cannot fund an agent.
        generator_proof = _block_hash(31)
        resp = await client.post(
            _UPLOAD_URL,
            json=_with_proof(_upload_body(_MINER, _archive("g2")), generator_proof),
        )
        assert resp.status_code == 200, resp.text
        other_agent = uuid4()
        async with session_maker() as session:
            with pytest.raises(PaymentReplayedError, match="generator upload"):
                async with session.begin():
                    await insert_agent(
                        session,
                        agent_id=other_agent,
                        miner_hotkey=_MINER.ss58_address,
                        name="fee-agent-2",
                        sha256="cd" * 32,
                        size_bytes=10,
                    )
                    await insert_evaluation_payment(
                        session,
                        verified=VerifiedPayment(
                            block_hash="0x" + generator_proof[2:].upper(),
                            extrinsic_index=1,
                            miner_hotkey=_MINER.ss58_address,
                            miner_coldkey=_MINER_COLDKEY,
                            amount_rao=40_000_000,
                            tao_usd_rate=None,
                            dest_address=app.state.config.upload_payment_address,
                            block_timestamp=datetime.now(UTC),
                        ),
                        agent_id=other_agent,
                    )
        assert await _count(session_maker, EvaluationPayment) == 1

    async def test_rejected_payment_spends_nothing(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        app.state.payment_verifier = _fake_verifier(
            error=PaymentAmountMismatch("paid the wrong amount")
        )
        await _open_lane(client, upload_fee_required=True)
        proof = _block_hash(40)
        body = _with_proof(_upload_body(_MINER, _archive("m")), proof)
        resp = await client.post(_UPLOAD_URL, json=body)
        assert resp.status_code == 402, resp.text
        assert resp.json()["error_code"] == 3203
        assert await _count(session_maker, GeneratorSubmission) == 0

        # The same proof stays usable once it verifies.
        app.state.payment_verifier = _fake_verifier()
        resp = await client.post(_UPLOAD_URL, json=body)
        assert resp.status_code == 200, resp.text

    async def test_a_stale_payment_cannot_fund_an_upload(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        app.state.payment_verifier = _fake_verifier(
            paid_at=datetime.now(UTC) - timedelta(hours=25)
        )
        await _open_lane(client, upload_fee_required=True)
        resp = await client.post(
            _UPLOAD_URL,
            json=_with_proof(_upload_body(_MINER, _archive("old")), _block_hash(50)),
        )
        assert resp.status_code == 402, resp.text
        assert resp.json()["error_code"] == 3208
        assert await _count(session_maker, GeneratorSubmission) == 0

    async def test_a_doomed_upload_is_refused_before_the_chain(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        verifier = _fake_verifier()
        app.state.payment_verifier = verifier
        await _open_lane(client, upload_fee_required=True)
        resp = await client.post(
            _UPLOAD_URL,
            json=_with_proof(_upload_body(_MINER, _archive("c1")), _block_hash(60)),
        )
        assert resp.status_code == 200, resp.text
        resp = await client.post(
            _UPLOAD_URL,
            json=_with_proof(_upload_body(_MINER, _archive("c2")), _block_hash(61)),
        )
        assert resp.status_code == 429, resp.text
        assert verifier.verify_payment.await_count == 1
