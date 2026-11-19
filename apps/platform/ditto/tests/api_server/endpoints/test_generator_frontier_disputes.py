"""Frontier disputes: a column owner disputes an oracle-failed case with TAO.

A case the panel and the frontier oracle both fail stays ``unsolved``. With
disputes on, the column's own hotkey reads which of its cases the oracle
failed, and may dispute one with the hop notes, a short reasoning and a TAO
deposit verified like the generator upload fee. An operator rules the
dispute: upheld, the case is attested and settled through ``apply_frontier``
like an oracle pass and the whole deposit is owed back; rejected, the deposit
is kept and the reason is shown to the miner. The lane is driven end to end
through the miner, validator and admin endpoints against a real Postgres.
"""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.generator import GeneratorCompetitionSettings
from ditto.api_models.generator_rounds import (
    frontier_dispute_confirmation,
    frontier_dispute_proof_sha256,
    frontier_dispute_signature_payload,
    frontier_refund_paid_confirmation,
    frontier_report_signature_payload,
)
from ditto.api_server.generator_frontier import STATE_AUDIT_PENDING, STATE_SETTLED
from ditto.api_server.generator_rounds import get_round
from ditto.api_server.payment_verifier import (
    PaymentProof,
    PaymentReplayedError,
    PaymentSignerMismatch,
    VerifiedPayment,
)
from ditto.db.models import (
    GeneratorFrontierDispute,
    GeneratorOperatorAction,
    GeneratorRoundCaseVerdict,
    GeneratorSubmission,
)
from ditto.db.queries.payments import insert_generator_payment
from ditto.tests.api_server.endpoints.test_generator_frontier import (
    _ROUNDS_URL,
    _Lane,
)
from ditto.tests.api_server.endpoints.test_generator_rounds import (
    _ADMIN,
    round_capable_fleet,  # noqa: F401 - autouse: one round-capable validator
)

_MINER = bittensor.Keypair.create_from_uri("//Bob")
_STRANGER = bittensor.Keypair.create_from_uri("//Charlie")
_MINER_COLDKEY = "5MinerColdkeyPlaceholderXXXXXXXXXXXXXXXXXXXXXXX"
_DISPUTES_URL = "/api/v1/admin/generator-competition/frontier/disputes"
_REFUNDS_URL = "/api/v1/admin/generator-competition/frontier/dispute-refunds"
_REASONING = "old7 names the locker; new7 says its code changed to the gold value"
_RAO = 1_000_000_000
_MIN = 50_000_000  # 0.05 TAO, the shipped minimum deposit

# 6 of 8 frontier at 1.5 credit, 2 unsolved: the score with no dispute.
_BASE = 9 / (1.5 * 8) * (6 / 8) ** 2
# One upheld dispute more: 7 of 8 frontier, 1 unsolved.
_ONE_UPHELD = 10.5 / (1.5 * 8) * (7 / 8) ** 2


def _fake_verifier(*, error: Exception | None = None) -> MagicMock:
    """Accepts any proof as a finalized transfer of exactly the stated
    amount from the hotkey's coldkey."""

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
            block_timestamp=datetime.now(UTC),
        )

    verifier = MagicMock()
    verifier.verify_payment = AsyncMock(side_effect=_verify)
    return verifier


def _dispute(
    round_index: int,
    case_id: str,
    *,
    evidence: list[str],
    amount_rao: int = _MIN,
    block_hash: str | None = None,
    signer: bittensor.Keypair = _MINER,
    hotkey: str | None = None,
    reasoning: str = _REASONING,
) -> dict[str, Any]:
    hotkey = hotkey or signer.ss58_address
    block_hash = block_hash or "0x" + secrets.token_hex(32)
    timestamp, nonce = int(time.time()), uuid4()
    proof = frontier_dispute_proof_sha256(
        evidence,
        reasoning,
        payment_block_hash=block_hash,
        payment_extrinsic_index=3,
        payment_amount_rao=amount_rao,
    )
    message = frontier_dispute_signature_payload(
        hotkey=hotkey,
        round_index=round_index,
        case_id=case_id,
        proof_sha256=proof,
        timestamp=timestamp,
        nonce=nonce,
    )
    return {
        "hotkey": hotkey,
        "case_id": case_id,
        "evidence_pair_ids": evidence,
        "reasoning": reasoning,
        "payment_block_hash": block_hash,
        "payment_block_number": 4_000_000,
        "payment_extrinsic_index": 3,
        "payment_amount_rao": amount_rao,
        "signature": signer.sign(message).hex(),
        "signature_timestamp": timestamp,
        "signature_nonce": str(nonce),
    }


def _evidence(case_id: str) -> list[str]:
    i = case_id.rsplit("k", 1)[-1]
    return [f"old{i}", f"new{i}"]


class _DisputeLane(_Lane):
    async def setup_disputes(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch, **settings: Any
    ) -> None:
        settings = {
            "frontier_audit_cases": 0,
            "frontier_disputes_enabled": True,
            **settings,
        }
        await self.setup(app, monkeypatch, miner_hotkey=_MINER.ss58_address, **settings)
        self.verifier = _fake_verifier()
        app.state.payment_verifier = self.verifier

    async def oracle_fails(self, failed: int = 2) -> int:
        """Score a round; the oracle passes all but the last ``failed``."""
        round_index = await self.score_round()
        job = await self.lease_oracle()
        await self.report_oracle(
            job, set(self.candidates[: len(self.candidates) - failed])
        )
        return round_index

    async def reveal(self, round_index: int, *, ago: timedelta = timedelta()) -> None:
        """Reveal the round; ``ago`` also moves the oracle report back."""
        async with self.maker() as session, session.begin():
            row = await get_round(session, round_index, for_update=True)
            assert row is not None
            when = datetime.now(UTC) - ago
            row.revealed_at = when
            frontier = dict(row.frontier or {})
            frontier["audit_opened_at"] = when.isoformat()
            row.frontier = frontier

    async def file(self, round_index: int, payload: dict[str, Any]) -> httpx.Response:
        return await self.client.post(
            f"/api/v1/generator/rounds/{round_index}/frontier/disputes", json=payload
        )

    async def report(
        self, round_index: int, signer: bittensor.Keypair = _MINER, *, status: int = 200
    ) -> dict[str, Any]:
        timestamp, nonce = int(time.time()), uuid4()
        message = frontier_report_signature_payload(
            hotkey=_MINER.ss58_address,
            round_index=round_index,
            timestamp=timestamp,
            nonce=nonce,
        )
        resp = await self.client.post(
            f"/api/v1/generator/rounds/{round_index}/frontier/report",
            json={
                "hotkey": _MINER.ss58_address,
                "signature": signer.sign(message).hex(),
                "signature_timestamp": timestamp,
                "signature_nonce": str(nonce),
            },
        )
        assert resp.status_code == status, resp.text
        return resp.json()

    async def rule(
        self,
        round_index: int,
        case_id: str,
        *,
        upheld: bool,
        reason: str = "the two cited notes give the gold code",
        confirmation: str = "",
    ) -> httpx.Response:
        return await self.client.post(
            f"{_ROUNDS_URL}/{round_index}/frontier/disputes/{case_id}",
            headers=_ADMIN,
            json={
                "upheld": upheld,
                "reason": reason,
                "actor": "backroom:test",
                "confirmation": confirmation
                or frontier_dispute_confirmation(round_index, case_id, upheld),
            },
        )

    async def refunds(self, status: str = "pending") -> list[dict[str, Any]]:
        resp = await self.client.get(
            _REFUNDS_URL, headers=_ADMIN, params={"status": status}
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["refunds"]

    async def verdicts(self, round_index: int) -> dict[str, str]:
        async with self.maker() as session:
            return {
                v.case_id: v.status
                for v in await session.scalars(
                    select(GeneratorRoundCaseVerdict).where(
                        GeneratorRoundCaseVerdict.round_index == round_index,
                        GeneratorRoundCaseVerdict.column_id == self.miner_column,
                    )
                )
            }


@pytest.fixture
def lane(
    client: httpx.AsyncClient, session_maker: async_sessionmaker[AsyncSession]
) -> _DisputeLane:
    return _DisputeLane(client, session_maker)


def test_disputes_off_keeps_the_unchanged_round_signature() -> None:
    on = GeneratorCompetitionSettings(frontier_oracle_agent_id=uuid4())
    assert not on.frontier_disputes_active
    disputes = on.model_copy(update={"frontier_disputes_enabled": True})
    assert disputes.frontier_disputes_active
    assert disputes.measurement_params() != on.measurement_params()
    deposit = disputes.model_copy(update={"frontier_dispute_min_tao": 0.1})
    assert deposit.measurement_params() != disputes.measurement_params()
    # Disputes never run without frontier scoring.
    off = GeneratorCompetitionSettings(frontier_disputes_enabled=True)
    assert not off.frontier_disputes_active
    assert off.measurement_params() == off.score_params()


def test_a_column_cap_above_the_round_cap_is_refused() -> None:
    with pytest.raises(ValueError, match="frontier_dispute_max_per_column"):
        GeneratorCompetitionSettings(
            frontier_disputes_enabled=True,
            frontier_dispute_max_per_column=5,
            frontier_dispute_max_per_round=4,
        )


class TestFrontierDisputes:
    async def test_an_upheld_dispute_is_credited_and_refunded_in_full(
        self, app: FastAPI, lane: _DisputeLane, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await lane.setup_disputes(app, monkeypatch)
        round_index = await lane.oracle_fails(2)
        hard, other = lane.candidates[7], lane.candidates[6]
        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_AUDIT_PENDING
        assert frontier["dispute_policy"]["min_rao"] == _MIN

        # The report needs the hotkey's own signature.
        await lane.report(round_index, _STRANGER, status=400)
        # After the oracle runs the owner sees which cases it failed (ids and
        # scores only), but cannot dispute before the round is revealed, and
        # the lane waits instead of settling at once.
        report = await lane.report(round_index)
        assert report["oracle_failed"] == [
            {"case_id": other, "score": 0.0},
            {"case_id": hard, "score": 0.0},
        ]
        assert report["disputable"] == []
        assert report["blocker"] == "the round is not revealed yet"
        early = await lane.file(
            round_index, _dispute(round_index, hard, evidence=_evidence(hard))
        )
        assert early.status_code == 409, early.text
        lane.verifier.verify_payment.assert_not_awaited()
        assert await lane.tick() == []

        await lane.reveal(round_index)
        report = await lane.report(round_index)
        assert sorted(report["disputable"]) == sorted([hard, other])

        paid = 200_000_000  # 0.20 TAO: the minimum plus a priority part
        payload = _dispute(round_index, hard, evidence=_evidence(hard), amount_rao=paid)
        resp = await lane.file(round_index, payload)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "pending"
        assert resp.json()["amount_rao"] == paid
        # Verified like the generator upload fee: this hotkey's coldkey, the
        # stated amount, the same receiving address.
        kwargs = lane.verifier.verify_payment.await_args.kwargs
        assert kwargs["expected_hotkey"] == _MINER.ss58_address
        assert kwargs["expected_amount_rao"] == paid
        assert (
            kwargs["expected_send_address"] == app.state.config.upload_payment_address
        )
        # One dispute per case.
        again = await lane.file(
            round_index, _dispute(round_index, hard, evidence=_evidence(hard))
        )
        assert again.status_code == 409 and "already disputed" in again.text
        assert await lane.tick() == []

        detail = await lane.client.get(
            f"{_ROUNDS_URL}/{round_index}/frontier/disputes/{hard}", headers=_ADMIN
        )
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert [p["pair_id"] for p in body["cited"]] == _evidence(hard)
        assert body["cited_declared"] == _evidence(hard)[1:]
        assert body["case"]["expected_answer"] == "5007"
        assert body["dispute"]["reasoning"] == _REASONING

        bad = await lane.rule(round_index, hard, upheld=True, confirmation="RULE")
        assert bad.status_code == 409
        resp = await lane.rule(round_index, hard, upheld=True)
        assert resp.status_code == 200, resp.text
        frontier = resp.json()
        assert hard in frontier["attested"][lane.miner_column]
        (dispute,) = frontier["disputes"]
        assert dispute["status"] == "upheld" and dispute["refund_rao"] == paid
        # A ruled dispute is final.
        assert (await lane.rule(round_index, hard, upheld=False)).status_code == 409

        # The whole deposit, priority part included, is owed to the coldkey
        # that paid it, as an operator payout.
        (refund,) = await lane.refunds()
        assert refund["refund_rao"] == paid == refund["amount_rao"]
        assert refund["miner_coldkey"] == _MINER_COLDKEY
        assert refund["dispute_id"] == dispute["dispute_id"]
        url = f"{_REFUNDS_URL}/{refund['dispute_id']}/paid"
        record = {
            "refund_block_hash": "0x" + "ab" * 32,
            "refund_extrinsic_index": 7,
            "reason": "refund sent from the subnet wallet",
            "actor": "backroom:test",
            "confirmation": "RECORD",
        }
        assert (
            await lane.client.post(url, headers=_ADMIN, json=record)
        ).status_code == 409
        record["confirmation"] = frontier_refund_paid_confirmation(refund["dispute_id"])
        resp = await lane.client.post(url, headers=_ADMIN, json=record)
        assert resp.status_code == 200, resp.text
        assert resp.json()["refund_status"] == "paid"
        assert await lane.refunds() == []
        again_paid = await lane.client.post(url, headers=_ADMIN, json=record)
        assert again_paid.status_code == 409

        # The other oracle-failed case is still disputable while the window
        # is open, so the lane keeps waiting; it settles when the window shuts.
        assert await lane.tick() == []
        notes = await lane.tick(now=datetime.now(UTC) + timedelta(hours=6, minutes=1))
        assert notes and "no audit required" in notes[0]
        frontier = await lane.frontier(round_index)
        assert frontier["state"] == STATE_SETTLED
        assert hard in frontier["applied"][lane.miner_column]
        assert frontier["column_scores"][lane.miner_column] == pytest.approx(
            _ONE_UPHELD
        )
        verdicts = await lane.verdicts(round_index)
        assert verdicts[hard] == "frontier" and verdicts[other] == "unsolved"
        assert sum(1 for s in verdicts.values() if s == "frontier") == 7
        async with lane.maker() as session:
            sub = await session.get(GeneratorSubmission, lane.submission)
            assert sub is not None and sub.round_score == pytest.approx(_ONE_UPHELD)
            action = await session.scalar(
                select(GeneratorOperatorAction).where(
                    GeneratorOperatorAction.action == "frontier_dispute_upheld"
                )
            )
            assert action is not None
            assert action.subject == f"round:{round_index}:case:{hard}"
        late = await lane.file(
            round_index, _dispute(round_index, other, evidence=_evidence(other))
        )
        assert late.status_code == 409 and "settled" in late.text

    async def test_a_deposit_below_the_minimum_is_refused(
        self, app: FastAPI, lane: _DisputeLane, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await lane.setup_disputes(app, monkeypatch)
        round_index = await lane.oracle_fails(2)
        await lane.reveal(round_index)
        hard = lane.candidates[7]
        resp = await lane.file(
            round_index,
            _dispute(round_index, hard, evidence=_evidence(hard), amount_rao=_MIN - 1),
        )
        assert resp.status_code == 402 and "at least 50000000 rao" in resp.text
        # Refused before any chain read.
        lane.verifier.verify_payment.assert_not_awaited()
        resp = await lane.file(
            round_index,
            _dispute(round_index, hard, evidence=_evidence(hard), amount_rao=_MIN),
        )
        assert resp.status_code == 200, resp.text

    async def test_disputes_are_reviewed_by_deposit_then_time(
        self, app: FastAPI, lane: _DisputeLane, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await lane.setup_disputes(app, monkeypatch)
        round_index = await lane.oracle_fails(2)
        await lane.reveal(round_index)
        low, high = lane.candidates[6], lane.candidates[7]
        for case_id, amount in ((low, _MIN), (high, 3 * _MIN)):
            resp = await lane.file(
                round_index,
                _dispute(
                    round_index, case_id, evidence=_evidence(case_id), amount_rao=amount
                ),
            )
            assert resp.status_code == 200, resp.text
        queue = (await lane.client.get(_DISPUTES_URL, headers=_ADMIN)).json()
        # The larger deposit first, though it was filed later.
        assert [d["case_id"] for d in queue["disputes"]] == [high, low]
        assert [d["amount_rao"] for d in queue["disputes"]] == [3 * _MIN, _MIN]
        assert (queue["count"], queue["has_more"]) == (2, False)

    async def test_the_payment_proof_is_checked(
        self,
        app: FastAPI,
        lane: _DisputeLane,
        monkeypatch: pytest.MonkeyPatch,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await lane.setup_disputes(app, monkeypatch)
        round_index = await lane.oracle_fails(2)
        await lane.reveal(round_index)
        hard, other = lane.candidates[7], lane.candidates[6]
        # A transfer signed by some other coldkey is not this miner's deposit.
        app.state.payment_verifier = _fake_verifier(
            error=PaymentSignerMismatch("signer is not the hotkey's coldkey")
        )
        resp = await lane.file(
            round_index, _dispute(round_index, hard, evidence=_evidence(hard))
        )
        assert resp.status_code >= 400, resp.text
        assert (await lane.frontier(round_index))["disputes"] == []
        app.state.payment_verifier = lane.verifier

        block = "0x" + "cd" * 32
        resp = await lane.file(
            round_index,
            _dispute(round_index, hard, evidence=_evidence(hard), block_hash=block),
        )
        assert resp.status_code == 200, resp.text
        # One transfer pays for one dispute.
        replay = await lane.file(
            round_index,
            _dispute(round_index, other, evidence=_evidence(other), block_hash=block),
        )
        assert replay.status_code == 402, replay.text
        assert replay.json()["message"] == "payment proof already used"
        # ...and never also funds an upload.
        async with session_maker() as session, session.begin():
            deposit = await session.scalar(select(GeneratorFrontierDispute))
            assert deposit is not None and deposit.refund_status == "none"
            with pytest.raises(PaymentReplayedError):
                await insert_generator_payment(
                    session,
                    verified=VerifiedPayment(
                        block_hash=block,
                        extrinsic_index=3,
                        miner_hotkey=_MINER.ss58_address,
                        miner_coldkey=_MINER_COLDKEY,
                        amount_rao=_MIN,
                        tao_usd_rate=None,
                        dest_address="5Dest",
                        block_timestamp=datetime.now(UTC),
                    ),
                    submission_id=lane.submission,
                )

    async def test_a_rejected_dispute_keeps_the_deposit_and_shows_the_reason(
        self, app: FastAPI, lane: _DisputeLane, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await lane.setup_disputes(app, monkeypatch, frontier_dispute_max_per_column=1)
        round_index = await lane.oracle_fails(2)
        await lane.reveal(round_index)
        hard, other = lane.candidates[7], lane.candidates[6]
        resp = await lane.file(
            round_index, _dispute(round_index, hard, evidence=_evidence(hard))
        )
        assert resp.status_code == 200, resp.text
        # A rejection must say why.
        silent = await lane.rule(round_index, hard, upheld=False, reason="no")
        assert silent.status_code == 422
        why = "new7 is a decoy note; the gold code is superseded by a later note"
        resp = await lane.rule(round_index, hard, upheld=False, reason=why)
        assert resp.status_code == 200, resp.text
        # The deposit is kept: no refund is owed.
        assert await lane.refunds() == []
        (kept,) = await lane.refunds("none")
        assert kept["amount_rao"] == _MIN and kept["refund_rao"] is None
        # The miner reads the ruling and its reason.
        report = await lane.report(round_index)
        (dispute,) = report["disputes"]
        assert dispute["status"] == "rejected" and dispute["reason"] == why
        # The column's one dispute is spent: no second dispute, no retry.
        capped = await lane.file(
            round_index, _dispute(round_index, other, evidence=_evidence(other))
        )
        assert capped.status_code == 409 and "used its 1 disputes" in capped.text
        # Nothing left to dispute: the lane settles at once, at the base score.
        notes = await lane.tick()
        assert notes and "no audit required" in notes[0]
        frontier = await lane.frontier(round_index)
        assert frontier["column_scores"][lane.miner_column] == pytest.approx(_BASE)
        assert (await lane.verdicts(round_index))[hard] == "unsolved"

    async def test_the_round_cap_counts_every_columns_disputes(
        self, app: FastAPI, lane: _DisputeLane, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await lane.setup_disputes(
            app,
            monkeypatch,
            frontier_dispute_max_per_column=2,
            frontier_dispute_max_per_round=2,
        )
        round_index = await lane.oracle_fails(2)
        await lane.reveal(round_index)
        # Another column's disputes already fill the round's budget.
        async with lane.maker() as session, session.begin():
            row = await get_round(session, round_index, for_update=True)
            assert row is not None
            frontier = dict(row.frontier or {})
            frontier["disputes"] = {
                f"zz/k{i}": {"column_id": "zz", "case_id": f"zz/k{i}"} for i in range(2)
            }
            row.frontier = frontier
        hard = lane.candidates[7]
        resp = await lane.file(
            round_index, _dispute(round_index, hard, evidence=_evidence(hard))
        )
        assert resp.status_code == 409 and "round used its 2 disputes" in resp.text
        lane.verifier.verify_payment.assert_not_awaited()

    async def test_a_column_the_oracle_mostly_beat_cannot_dispute(
        self, app: FastAPI, lane: _DisputeLane, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await lane.setup_disputes(app, monkeypatch)
        # 3 of 8 oracle failures is above the 25% flood guard.
        round_index = await lane.oracle_fails(3)
        await lane.reveal(round_index)
        hard = lane.candidates[7]
        resp = await lane.file(
            round_index, _dispute(round_index, hard, evidence=_evidence(hard))
        )
        assert resp.status_code == 409 and "oracle failed 3/8" in resp.text
        report = await lane.report(round_index)
        assert report["disputable"] == [] and "oracle failed 3/8" in report["blocker"]
        # Nothing disputable: the lane does not wait for the window.
        notes = await lane.tick()
        assert notes and "no audit required" in notes[0]

    async def test_only_the_columns_hotkey_may_dispute(
        self, app: FastAPI, lane: _DisputeLane, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await lane.setup_disputes(app, monkeypatch)
        round_index = await lane.oracle_fails(2)
        await lane.reveal(round_index)
        hard = lane.candidates[7]
        stranger = await lane.file(
            round_index,
            _dispute(round_index, hard, evidence=_evidence(hard), signer=_STRANGER),
        )
        assert stranger.status_code == 403, stranger.text
        # The owner's hotkey with another key's signature does not verify.
        forged = await lane.file(
            round_index,
            _dispute(
                round_index,
                hard,
                evidence=_evidence(hard),
                signer=_STRANGER,
                hotkey=_MINER.ss58_address,
            ),
        )
        assert forged.status_code == 400
        # The signature covers the proof and the deposit.
        payload = _dispute(round_index, hard, evidence=_evidence(hard))
        payload["evidence_pair_ids"] = ["new7"]
        assert (await lane.file(round_index, payload)).status_code == 400
        payload = _dispute(round_index, hard, evidence=_evidence(hard))
        payload["payment_amount_rao"] = 2 * _MIN
        assert (await lane.file(round_index, payload)).status_code == 400
        assert (await lane.frontier(round_index))["disputes"] == []
        lane.verifier.verify_payment.assert_not_awaited()

    async def test_faulted_passed_and_unheld_evidence_are_refused(
        self, app: FastAPI, lane: _DisputeLane, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await lane.setup_disputes(app, monkeypatch)
        # The oracle fails k6 and faults k7: k7 was never answered.
        round_index = await lane.score_round()
        job = await lane.lease_oracle()
        faulted = lane.candidates[7]
        await lane.report_oracle(
            job, set(lane.candidates[:6]), faulted=frozenset({faulted})
        )
        await lane.reveal(round_index)
        resp = await lane.file(
            round_index, _dispute(round_index, faulted, evidence=_evidence(faulted))
        )
        assert resp.status_code == 409 and "did not answer and fail" in resp.text
        attested = lane.candidates[0]
        resp = await lane.file(
            round_index, _dispute(round_index, attested, evidence=_evidence(attested))
        )
        assert resp.status_code == 409 and "already attested" in resp.text
        failed = lane.candidates[6]
        resp = await lane.file(
            round_index, _dispute(round_index, failed, evidence=["new6", "nowhere"])
        )
        assert resp.status_code == 422 and "nowhere" in resp.text
        report = await lane.report(round_index)
        assert report["disputable"] == [failed]
        assert [r["case_id"] for r in report["oracle_failed"]] == [failed]
        lane.verifier.verify_payment.assert_not_awaited()

    async def test_disputes_close_with_the_window(
        self, app: FastAPI, lane: _DisputeLane, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await lane.setup_disputes(app, monkeypatch, frontier_dispute_window_hours=2)
        round_index = await lane.oracle_fails(2)
        await lane.reveal(round_index, ago=timedelta(hours=2, minutes=1))
        hard = lane.candidates[7]
        resp = await lane.file(
            round_index, _dispute(round_index, hard, evidence=_evidence(hard))
        )
        assert resp.status_code == 409 and "dispute window closed" in resp.text
        notes = await lane.tick()
        assert notes and "no audit required" in notes[0]
        frontier = await lane.frontier(round_index)
        assert frontier["column_scores"][lane.miner_column] == pytest.approx(_BASE)

    async def test_an_unruled_dispute_lapses_and_is_refunded(
        self, app: FastAPI, lane: _DisputeLane, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await lane.setup_disputes(app, monkeypatch, frontier_audit_grace_hours=8)
        round_index = await lane.oracle_fails(2)
        await lane.reveal(round_index)
        hard = lane.candidates[7]
        resp = await lane.file(
            round_index,
            _dispute(round_index, hard, evidence=_evidence(hard), amount_rao=2 * _MIN),
        )
        assert resp.status_code == 200, resp.text
        # A pending dispute holds the lane past its window, up to the grace.
        assert await lane.tick(now=datetime.now(UTC) + timedelta(hours=7)) == []
        notes = await lane.tick(now=datetime.now(UTC) + timedelta(hours=8, minutes=1))
        assert notes and "audit grace elapsed" in notes[0]
        frontier = await lane.frontier(round_index)
        assert frontier["disputes"][0]["status"] == "expired"
        assert frontier["column_scores"][lane.miner_column] == pytest.approx(_BASE)
        # Operator silence never costs the miner its deposit.
        (refund,) = await lane.refunds()
        assert refund["refund_rao"] == 2 * _MIN

    async def test_disputes_off_takes_no_dispute(
        self, app: FastAPI, lane: _DisputeLane, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await lane.setup_disputes(app, monkeypatch, frontier_disputes_enabled=False)
        round_index = await lane.oracle_fails(2)
        await lane.reveal(round_index)
        frontier = await lane.frontier(round_index)
        assert frontier["dispute_policy"] is None and frontier["disputes"] == []
        hard = lane.candidates[7]
        resp = await lane.file(
            round_index, _dispute(round_index, hard, evidence=_evidence(hard))
        )
        assert resp.status_code == 409 and "takes no disputes" in resp.text
        # Exactly the old lane: it settles at once.
        notes = await lane.tick()
        assert notes and "no audit required" in notes[0]
