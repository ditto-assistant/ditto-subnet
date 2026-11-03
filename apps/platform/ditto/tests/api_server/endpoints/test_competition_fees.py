"""Per-competition submission fees (SN-349).

The submission settings carry a default fee (the memory/agent lane's price)
and per-competition overrides. A competition without an override inherits
the default amount and its own fee switch, so nothing changes until an
operator prices a competition; once priced, the generator lane charges and
verifies its own amount while the agent lane keeps the default.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.submission_settings import (
    AdminSubmissionSettingsRequest,
    CompetitionFeePolicy,
    competition_fees_confirmation,
)
from ditto.api_server.payment_verifier import (
    PaymentAmountMismatch,
    PaymentProof,
    VerifiedPayment,
)
from ditto.db.models import GeneratorSubmission, GeneratorSubmissionPayment
from ditto.db.queries.agents import insert_agent
from ditto.db.queries.payments import insert_evaluation_payment
from ditto.db.queries.submission_settings import (
    CompetitionFeeOverride,
    EffectiveSubmissionSettings,
    parse_competition_fees,
)
from ditto.tests.api_server.endpoints.test_generator_competition import (
    _ADMIN,
    _MINER,
    _MINER_COLDKEY,
    _archive,
    _install,
    _open_lane,
    _upload_body,
)

_SUBMISSION_SETTINGS_URL = "/api/v1/admin/submission-settings"
_UPLOAD_URL = "/api/v1/generator/submissions"
_TERMS_URL = "/api/v1/generator/upload-terms"
_PRICING_URL = "/api/v1/upload/eval-pricing"
_GENERATOR_FEE = 500_000_000


def _block_hash(salt: int) -> str:
    return "0x" + format(salt, "064x")


def _with_proof(body: dict[str, Any], block_hash: str) -> dict[str, Any]:
    return {
        **body,
        "payment_block_hash": block_hash,
        "payment_block_number": 4242,
        "payment_extrinsic_index": 1,
    }


def _chain_verifier(*, paid_rao: int) -> MagicMock:
    """A verifier whose transfer paid exactly ``paid_rao``, checked like the
    real one: any other expected amount is a :class:`PaymentAmountMismatch`."""

    async def _verify(
        proof: PaymentProof,
        expected_hotkey: str,
        expected_amount_rao: int,
        legacy_amount_cutoff_at: datetime | None = None,  # noqa: ARG001
        expected_send_address: str | None = None,
    ) -> VerifiedPayment:
        if paid_rao != expected_amount_rao:
            raise PaymentAmountMismatch(
                f"paid {paid_rao} rao, expected {expected_amount_rao} rao"
            )
        assert expected_send_address is not None
        return VerifiedPayment(
            block_hash=proof.block_hash,
            extrinsic_index=proof.extrinsic_index,
            miner_hotkey=expected_hotkey,
            miner_coldkey=_MINER_COLDKEY,
            amount_rao=paid_rao,
            tao_usd_rate=Decimal("400"),
            dest_address=expected_send_address,
            block_timestamp=datetime.now(UTC),
        )

    verifier = MagicMock()
    verifier.verify_payment = AsyncMock(side_effect=_verify)
    return verifier


async def _submission_settings(client: httpx.AsyncClient) -> dict[str, Any]:
    resp = await client.get(_SUBMISSION_SETTINGS_URL, headers=_ADMIN)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _confirmation(
    cooldown: int, fee: int, competition_fees: dict[str, Any] | None
) -> str:
    phrase = f"SET SUBMISSION COOLDOWN {cooldown} SECONDS FEE {fee} RAO"
    if competition_fees is None:
        return phrase
    return phrase + competition_fees_confirmation(
        {
            competition: CompetitionFeePolicy(**policy)
            for competition, policy in competition_fees.items()
        }
    )


async def _price(
    client: httpx.AsyncClient,
    competition_fees: dict[str, Any] | None,
    *,
    fee_amount_rao: int | None = None,
    confirmation: str | None = None,
) -> httpx.Response:
    current = (await _submission_settings(client))["current"]
    fee = fee_amount_rao or current["fee_amount_rao"]
    body: dict[str, Any] = {
        "expected_revision": current["revision"],
        "cooldown_seconds": current["cooldown_seconds"],
        "fee_amount_rao": fee,
        "reason": "price the submission competitions",
        "actor": "backroom:test",
        "confirmation": confirmation
        or _confirmation(current["cooldown_seconds"], fee, competition_fees),
    }
    if competition_fees is not None:
        body["competition_fees"] = competition_fees
    return await client.post(_SUBMISSION_SETTINGS_URL, headers=_ADMIN, json=body)


async def _count(maker: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with maker() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


def _effective(settings: dict[str, Any], competition: str) -> dict[str, Any]:
    return next(
        row
        for row in settings["effective_competition_fees"]
        if row["competition"] == competition
    )


class TestResolution:
    def test_null_inherits_and_overrides_win(self) -> None:
        settings = EffectiveSubmissionSettings(
            revision=4,
            cooldown_seconds=3600,
            payment_address="addr",
            fee_amount_rao=200_000_000,
            competition_fees={},
        )
        inherited = settings.competition_fee("generator", competition_requires_fee=True)
        assert inherited.fee_amount_rao == 200_000_000
        assert inherited.amount_source == "default"
        assert inherited.fee_required is True
        assert inherited.required_source == "competition_settings"

        priced = EffectiveSubmissionSettings(
            revision=5,
            cooldown_seconds=3600,
            payment_address="addr",
            fee_amount_rao=200_000_000,
            competition_fees={
                "generator": CompetitionFeeOverride(
                    fee_amount_rao=_GENERATOR_FEE, fee_required=True
                )
            },
        )
        generator = priced.competition_fee("generator", competition_requires_fee=False)
        assert generator.fee_amount_rao == _GENERATOR_FEE
        assert generator.amount_source == "override"
        assert generator.fee_required is True
        assert generator.required_source == "override"
        # The agent lane keeps the default whatever the generator costs.
        memory = priced.competition_fee("memory", competition_requires_fee=False)
        assert memory.fee_amount_rao == 200_000_000
        assert memory.fee_required is True
        assert memory.required_source == "always"

    def test_stored_maps_fail_toward_inheritance(self) -> None:
        assert parse_competition_fees({}) == {}
        assert parse_competition_fees(None) == {}
        assert parse_competition_fees(
            {
                "generator": {"fee_amount_rao": 7, "fee_required": None},
                "unknown_track": {"fee_amount_rao": 1},
                "coding": "not an object",
                "router": {"fee_amount_rao": True, "fee_required": "yes"},
            }
        ) == {"generator": CompetitionFeeOverride(fee_amount_rao=7)}

    def test_request_refuses_memory_and_unknown_and_normalizes(self) -> None:
        base: dict[str, Any] = {
            "expected_revision": 1,
            "cooldown_seconds": 3600,
            "reason": "price the competitions",
            "confirmation": "x",
        }
        bad: dict[str, Any]
        for bad in ({"memory": {"fee_amount_rao": 5}}, {"generater": {}}):
            with pytest.raises(ValueError):
                AdminSubmissionSettingsRequest.model_validate(
                    {**base, "competition_fees": bad}
                )
        request = AdminSubmissionSettingsRequest.model_validate(
            {
                **base,
                "competition_fees": {
                    "router": {"fee_amount_rao": None, "fee_required": None},
                    "generator": {"fee_amount_rao": 9},
                },
            }
        )
        assert request.competition_fees == {
            "generator": CompetitionFeePolicy(fee_amount_rao=9)
        }
        assert competition_fees_confirmation(request.competition_fees) == (
            "; GENERATOR FEE 9 RAO REQUIRED INHERIT"
        )
        assert competition_fees_confirmation({}) == "; ALL COMPETITIONS INHERIT"


class TestAdminSettings:
    async def test_defaults_inherit_everything(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        settings = await _submission_settings(client)
        assert settings["current"]["competition_fees"] == {}
        default_fee = settings["current"]["fee_amount_rao"]
        assert _effective(settings, "memory") == {
            "competition": "memory",
            "fee_amount_rao": default_fee,
            "amount_source": "default",
            "fee_required": True,
            "required_source": "always",
        }
        assert _effective(settings, "generator") == {
            "competition": "generator",
            "fee_amount_rao": default_fee,
            "amount_source": "default",
            "fee_required": False,
            "required_source": "competition_settings",
        }

    async def test_override_is_confirmed_carried_forward_and_cleared(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        override = {"generator": {"fee_amount_rao": _GENERATOR_FEE}}
        current = (await _submission_settings(client))["current"]

        # The phrase must echo the override.
        stale_phrase = await _price(
            client,
            override,
            confirmation=_confirmation(
                current["cooldown_seconds"], current["fee_amount_rao"], None
            ),
        )
        assert stale_phrase.status_code == 409, stale_phrase.text
        assert "GENERATOR FEE 500000000 RAO REQUIRED INHERIT" in stale_phrase.text

        resp = await _price(client, override)
        assert resp.status_code == 200, resp.text
        assert resp.json()["competition_fees"] == {
            "generator": {"fee_amount_rao": _GENERATOR_FEE, "fee_required": None}
        }
        settings = await _submission_settings(client)
        generator = _effective(settings, "generator")
        assert generator["fee_amount_rao"] == _GENERATOR_FEE
        assert generator["amount_source"] == "override"

        # A write that omits the map (an older client, the cooldown panel)
        # keeps the old phrase and carries the override forward.
        resp = await _price(client, None, fee_amount_rao=300_000_000)
        assert resp.status_code == 200, resp.text
        settings = await _submission_settings(client)
        assert settings["current"]["fee_amount_rao"] == 300_000_000
        assert _effective(settings, "generator")["fee_amount_rao"] == _GENERATOR_FEE
        assert _effective(settings, "memory")["fee_amount_rao"] == 300_000_000

        # An explicit empty map clears every override.
        resp = await _price(client, {})
        assert resp.status_code == 200, resp.text
        settings = await _submission_settings(client)
        assert settings["current"]["competition_fees"] == {}
        assert _effective(settings, "generator")["fee_amount_rao"] == 300_000_000


class TestGeneratorLane:
    async def test_inherits_the_agent_fee_until_priced(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client, upload_fee_required=True)
        agent_fee = (await client.get(_PRICING_URL)).json()["amount_rao"]
        terms = (await client.get(_TERMS_URL)).json()
        assert terms["competition"] == "generator"
        assert terms["payment_amount_rao"] == agent_fee
        assert terms["payment_amount_source"] == "default"

    async def test_override_prices_generator_only(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client, upload_fee_required=True)
        agent_fee = (await client.get(_PRICING_URL)).json()["amount_rao"]
        assert agent_fee != _GENERATOR_FEE
        resp = await _price(client, {"generator": {"fee_amount_rao": _GENERATOR_FEE}})
        assert resp.status_code == 200, resp.text

        terms = (await client.get(_TERMS_URL)).json()
        assert terms["payment_required"] is True
        assert terms["payment_amount_rao"] == _GENERATOR_FEE
        assert terms["payment_amount_source"] == "override"
        # The agent lane's quote is untouched.
        assert (await client.get(_PRICING_URL)).json()["amount_rao"] == agent_fee

        app.state.payment_verifier = _chain_verifier(paid_rao=_GENERATOR_FEE)
        resp = await client.post(
            _UPLOAD_URL,
            json=_with_proof(_upload_body(_MINER, _archive("priced")), _block_hash(1)),
        )
        assert resp.status_code == 200, resp.text
        async with session_maker() as session:
            payment = await session.scalar(select(GeneratorSubmissionPayment))
            assert payment is not None
            assert payment.amount_rao == _GENERATOR_FEE

    @pytest.mark.parametrize("paid_offset", [1])
    async def test_a_transfer_of_any_other_amount_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        paid_offset: int,
    ) -> None:
        """Underpayment is refused; so is overpayment (the fee is exact)."""
        _install(app, session_maker)
        await _open_lane(client, upload_fee_required=True)
        resp = await _price(client, {"generator": {"fee_amount_rao": _GENERATOR_FEE}})
        assert resp.status_code == 200, resp.text
        app.state.payment_verifier = _chain_verifier(
            paid_rao=_GENERATOR_FEE + paid_offset
        )
        resp = await client.post(
            _UPLOAD_URL,
            json=_with_proof(_upload_body(_MINER, _archive("short")), _block_hash(2)),
        )
        assert resp.status_code == 402, resp.text
        assert resp.json()["error_code"] == 3203
        assert await _count(session_maker, GeneratorSubmission) == 0
        assert await _count(session_maker, GeneratorSubmissionPayment) == 0

    async def test_a_transfer_sized_for_the_agent_fee_cannot_fund_it(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client, upload_fee_required=True)
        agent_fee = (await client.get(_PRICING_URL)).json()["amount_rao"]
        resp = await _price(client, {"generator": {"fee_amount_rao": _GENERATOR_FEE}})
        assert resp.status_code == 200, resp.text

        # A fresh transfer of the agent fee is the wrong amount.
        app.state.payment_verifier = _chain_verifier(paid_rao=agent_fee)
        resp = await client.post(
            _UPLOAD_URL,
            json=_with_proof(_upload_body(_MINER, _archive("agent")), _block_hash(3)),
        )
        assert resp.status_code == 402, resp.text
        assert resp.json()["error_code"] == 3203

        # An unspent agent credit is a replay in the generator lane even when
        # the generator price would match it.
        credit = _block_hash(4)
        credited_agent = uuid4()
        async with session_maker() as session, session.begin():
            await insert_agent(
                session,
                agent_id=credited_agent,
                miner_hotkey=_MINER.ss58_address,
                name="credited-agent",
                sha256="ef" * 32,
                size_bytes=10,
            )
            await insert_evaluation_payment(
                session,
                verified=VerifiedPayment(
                    block_hash=credit,
                    extrinsic_index=1,
                    miner_hotkey=_MINER.ss58_address,
                    miner_coldkey=_MINER_COLDKEY,
                    amount_rao=_GENERATOR_FEE,
                    tao_usd_rate=None,
                    dest_address=app.state.config.upload_payment_address,
                    block_timestamp=datetime.now(UTC),
                ),
                credit_for_agent_id=credited_agent,
            )
        app.state.payment_verifier = _chain_verifier(paid_rao=_GENERATOR_FEE)
        resp = await client.post(
            _UPLOAD_URL,
            json=_with_proof(_upload_body(_MINER, _archive("credit")), credit),
        )
        assert resp.status_code == 402, resp.text
        assert resp.json()["error_code"] == 3207
        assert await _count(session_maker, GeneratorSubmission) == 0

    async def test_fee_required_override_beats_the_generator_switch(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client, upload_fee_required=False)
        resp = await _price(
            client,
            {"generator": {"fee_amount_rao": _GENERATOR_FEE, "fee_required": True}},
        )
        assert resp.status_code == 200, resp.text
        terms = (await client.get(_TERMS_URL)).json()
        assert terms["payment_required"] is True
        assert terms["payment_amount_rao"] == _GENERATOR_FEE
        resp = await client.post(
            _UPLOAD_URL, json=_upload_body(_MINER, _archive("unpaid"))
        )
        assert resp.status_code == 402, resp.text

        # And an explicit "free" waives a fee the generator policy requires.
        await _open_lane(client, upload_fee_required=True)
        resp = await _price(client, {"generator": {"fee_required": False}})
        assert resp.status_code == 200, resp.text
        terms = (await client.get(_TERMS_URL)).json()
        assert terms["payment_required"] is False
        assert terms["payment_amount_rao"] is None
        verifier = _chain_verifier(paid_rao=_GENERATOR_FEE)
        app.state.payment_verifier = verifier
        resp = await client.post(
            _UPLOAD_URL, json=_upload_body(_MINER, _archive("free"))
        )
        assert resp.status_code == 200, resp.text
        verifier.verify_payment.assert_not_awaited()


class TestSubmissionTerms:
    async def test_every_competition_reports_its_deliverable_and_fee(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client, upload_fee_required=True)
        agent_fee = (await client.get(_PRICING_URL)).json()
        resp = await client.get("/api/v1/public/submission-terms")
        assert resp.status_code == 200, resp.text
        terms = {row["competition"]: row for row in resp.json()["competitions"]}
        assert set(terms) == {"memory", "generator"}

        memory = terms["memory"]
        assert memory["deliverable"]["kind"] == "agent_harness_tarball"
        assert memory["deliverable"]["required_files"] == ["Dockerfile"]
        limits = {limit["name"]: limit for limit in memory["deliverable"]["limits"]}
        assert limits["max_archive_bytes"]["reference"] == (
            "DITTO_MAX_TARBALL_SIZE_BYTES"
        )
        assert limits["max_source_file_lines"]["source"] == "code"
        assert memory["cooldown"]["scope"] == "owner_coldkey"
        assert memory["fee"]["required"] is True
        assert memory["fee"]["amount_rao"] == agent_fee["amount_rao"]
        assert memory["fee"]["send_address"] == agent_fee["send_address"]

        generator = terms["generator"]
        assert generator["accepting"] is True
        assert generator["deliverable"]["contract_version"] == (
            "dittobench-generator/v1"
        )
        limits = {limit["name"]: limit for limit in generator["deliverable"]["limits"]}
        assert limits["max_archive_bytes"]["value"] == 4 << 20
        assert limits["cases_per_seed"]["value"] == 8
        assert limits["cases_per_seed"]["source"] == "setting"
        assert limits["cases_per_seed"]["reference"] == (
            "generator_competition.cases_per_seed"
        )
        assert generator["cooldown"] == {
            "seconds": 60,
            "scope": "hotkey",
            "source": "setting",
            "reference": "generator_competition.upload_cooldown_seconds",
        }
        assert generator["fee"]["required"] is True
        assert generator["fee"]["amount_source"] == "default"
        assert generator["fee"]["amount_rao"] == agent_fee["amount_rao"]

        resp = await _price(client, {"generator": {"fee_amount_rao": _GENERATOR_FEE}})
        assert resp.status_code == 200, resp.text
        one = (await client.get("/api/v1/public/submission-terms/generator")).json()
        assert one["fee"]["amount_rao"] == _GENERATOR_FEE
        assert one["fee"]["amount_source"] == "override"
        memory = (await client.get("/api/v1/public/submission-terms/memory")).json()
        assert memory["fee"]["amount_rao"] == agent_fee["amount_rao"]
        missing = await client.get("/api/v1/public/submission-terms/coding")
        assert missing.status_code == 404
