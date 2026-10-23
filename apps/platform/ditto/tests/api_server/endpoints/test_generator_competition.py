"""Contract tests for the shadow generator competition lane.

Covers the operator settings board, signed pack uploads, the trusted scorer's
lease/result loop (panel snapshot, invalid packs, insufficient panels,
cross-pack duplicates), the public board, the validator ledger, and the
operator pack views. The scoring arithmetic itself is pinned by
``ditto/tests/test_generator_scoring.py`` against the Go golden vectors.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ditto.api_server.endpoints.generator_scorer as scorer_mod
from ditto.api_models.generator import (
    generator_settings_confirmation,
    pack_signature_payload,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.chain.models import NeuronInfo
from ditto.db.models import GeneratorCaseVerdict, GeneratorPack
from ditto.tests.api_server.endpoints.test_scoring import (
    _install_chain,
    _ledger_headers,
)

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_SCORER_TOKEN = "test-generator-scorer-token-32-characters"
_ADMIN = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_SCORER = {"Authorization": f"Bearer {_SCORER_TOKEN}"}
_MINER = bittensor.Keypair.create_from_uri("//Bob")
_OTHER_MINER = bittensor.Keypair.create_from_uri("//Charlie")
_MINER_COLDKEY = "5MinerColdkeyPlaceholderXXXXXXXXXXXXXXXXXXXXXXX"
_SETTINGS_URL = "/api/v1/admin/generator-competition/settings"


def _settings(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mode": "shadow",
        "panel_size": 4,
        "min_panel": 3,
        "min_solver_owners": 2,
        "solve_threshold": 0.5,
        "champion_bonus": 0.5,
        "target_cases": 8,
        "window_days": 7,
        "max_pending_per_hotkey": 2,
        "upload_cooldown_seconds": 60,
        "lease_ttl_seconds": 600,
        "max_attempts": 2,
        "retry_backoff_seconds": 60,
    }
    base.update(overrides)
    return base


def _install(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    *,
    registered: tuple[bittensor.Keypair, ...] = (_MINER, _OTHER_MINER),
) -> None:
    app.state.config = replace(
        app.state.config,
        admin_api_token=_ADMIN_TOKEN,
        generator_scorer_token=_SCORER_TOKEN,
    )
    app.state.session_maker = maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    async def _chain() -> MagicMock:
        chain = MagicMock()
        chain.get_recent_neurons = AsyncMock(
            return_value=[
                NeuronInfo(
                    hotkey=key.ss58_address,
                    coldkey=f"{_MINER_COLDKEY[:-2]}{index:02d}",
                    uid=index,
                    stake=0.0,
                    validator_permit=False,
                )
                for index, key in enumerate(registered)
            ]
        )
        return chain

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_chain_client] = _chain


async def _open_lane(client: httpx.AsyncClient, **overrides: Any) -> None:
    current = (await client.get(_SETTINGS_URL, headers=_ADMIN)).json()
    settings = _settings(**overrides)
    resp = await client.post(
        _SETTINGS_URL,
        headers=_ADMIN,
        json={
            "expected_revision": current["effective"]["revision"],
            "settings": settings,
            "reason": "open the shadow generator lane",
            "actor": "backroom:test",
            "confirmation": generator_settings_confirmation(settings["mode"]),
        },
    )
    assert resp.status_code == 200, resp.text


def _pack_bytes(hotkey: str, *, cases: int = 8, salt: str = "") -> bytes:
    doc = {
        "contract": "dittobench-challenge-pack/v1",
        "grading_bench_version": 13,
        "miner_hotkey": hotkey,
        "generator": {"name": "test-gen", "version": "1", "seed": 1},
        "memory_waves": [{"pairs": [{"pair_id": f"p-{salt}"}]}],
        "cases": [{"id": f"c{i}{salt}"} for i in range(cases)],
    }
    return json.dumps(doc).encode()


def _upload_body(
    key: bittensor.Keypair, raw: bytes, *, sha: str | None = None
) -> dict[str, Any]:
    digest = sha or hashlib.sha256(raw).hexdigest()
    nonce = uuid4()
    ts = int(time.time())
    signature = key.sign(
        pack_signature_payload(
            hotkey=key.ss58_address, pack_sha256=digest, timestamp=ts, nonce=nonce
        )
    ).hex()
    return {
        "hotkey": key.ss58_address,
        "pack_sha256": digest,
        "pack_base64": base64.b64encode(raw).decode(),
        "signature": signature,
        "signature_timestamp": ts,
        "signature_nonce": str(nonce),
    }


def _refs(raw: bytes) -> list[dict[str, str]]:
    doc = json.loads(raw)
    return [
        {
            "case_id": case["id"],
            "fingerprint": hashlib.sha256(case["id"].encode()).hexdigest(),
        }
        for case in doc["cases"]
    ]


_PANEL = [
    {"agent_id": "agent-champ", "owner_key": "coldkey:champ", "champion": True},
    {"agent_id": "agent-b", "owner_key": "coldkey:b", "champion": False},
    {"agent_id": "agent-c", "owner_key": "coldkey:c", "champion": False},
    {"agent_id": "agent-d", "owner_key": "coldkey:d", "champion": False},
]


@pytest.fixture
def fixed_panel(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    async def _select(_session: AsyncSession, *, size: int) -> list[dict[str, Any]]:
        return [dict(slot) for slot in _PANEL[:size]]

    monkeypatch.setattr(scorer_mod.generator_competition, "select_panel", _select)
    return _PANEL


def _results(
    refs: list[dict[str, str]], scores: dict[str, float], *, agents: list[str]
) -> list[dict[str, Any]]:
    return [
        {
            "agent_id": agent,
            "owner_key": "ignored-by-platform",
            "champion": False,
            "results": [
                {"case_id": ref["case_id"], "score": scores[agent]} for ref in refs
            ],
        }
        for agent in agents
    ]


async def _upload(client: httpx.AsyncClient, raw: bytes, key=_MINER) -> dict:
    resp = await client.post("/api/v1/generator/packs", json=_upload_body(key, raw))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _lease(client: httpx.AsyncClient) -> dict:
    resp = await client.get("/api/v1/generator-scorer/work", headers=_SCORER)
    assert resp.status_code == 200, resp.text
    return resp.json()["work"]


def test_signature_payload_is_the_pinned_wire_format() -> None:
    """Pinned literally here and in the miner CLI's ``test_generator``."""
    nonce = UUID("4b0d7e8e-5f3c-4c4e-9a8b-1e2f3a4b5c6d")
    payload = pack_signature_payload(
        hotkey=_MINER.ss58_address,
        pack_sha256="ab" * 32,
        timestamp=1_700_000_000,
        nonce=nonce,
    )
    expected = f"ditto-generator-pack-v1:{_MINER.ss58_address}:{'ab' * 32}"
    assert payload == f"{expected}:1700000000:{nonce}".encode()


class TestSettings:
    async def test_lane_ships_closed_and_writes_are_guarded(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        assert (await client.get(_SETTINGS_URL)).status_code == 401
        board = (await client.get(_SETTINGS_URL, headers=_ADMIN)).json()
        assert board["effective"]["settings"]["mode"] == "off"
        assert board["effective"]["source"] == "default"

        body = {
            "expected_revision": 0,
            "settings": _settings(),
            "reason": "open the shadow generator lane",
            "confirmation": "yes",
        }
        assert (
            await client.post(_SETTINGS_URL, headers=_ADMIN, json=body)
        ).status_code == 409
        partial = {**body, "settings": {"mode": "shadow"}}
        partial["confirmation"] = generator_settings_confirmation("shadow")
        assert (
            await client.post(_SETTINGS_URL, headers=_ADMIN, json=partial)
        ).status_code == 422
        body["confirmation"] = generator_settings_confirmation("shadow")
        stale = {**body, "expected_revision": 3}
        assert (
            await client.post(_SETTINGS_URL, headers=_ADMIN, json=stale)
        ).status_code == 409
        ok = await client.post(_SETTINGS_URL, headers=_ADMIN, json=body)
        assert ok.status_code == 200 and ok.json()["revision"] >= 1
        incoherent = {
            **body,
            "expected_revision": ok.json()["revision"],
            "settings": _settings(min_solver_owners=4, min_panel=3),
        }
        assert (
            await client.post(_SETTINGS_URL, headers=_ADMIN, json=incoherent)
        ).status_code == 422


class TestUpload:
    async def test_closed_lane_refuses_uploads(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        raw = _pack_bytes(_MINER.ss58_address)
        resp = await client.post(
            "/api/v1/generator/packs", json=_upload_body(_MINER, raw)
        )
        assert resp.status_code == 403

    async def test_signed_upload_is_queued_privately(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client)
        raw = _pack_bytes(_MINER.ss58_address)
        accepted = await _upload(client, raw)
        assert accepted["status"] == "queued" and accepted["case_count"] == 8
        async with session_maker() as session:
            row = await session.get(GeneratorPack, accepted["pack_id"])
            assert row is not None and row.pack_bytes == raw
            assert row.owner_key.startswith("coldkey:")
        listing = await client.get(
            "/api/v1/generator/packs", params={"hotkey": _MINER.ss58_address}
        )
        pack = listing.json()["packs"][0]
        assert pack["pack_sha256"] == hashlib.sha256(raw).hexdigest()
        assert "pack_base64" not in pack and "cases" not in pack

        dup = await client.post(
            "/api/v1/generator/packs", json=_upload_body(_MINER, raw)
        )
        assert dup.status_code == 409
        cooldown = await client.post(
            "/api/v1/generator/packs",
            json=_upload_body(_MINER, _pack_bytes(_MINER.ss58_address, salt="x")),
        )
        assert cooldown.status_code == 429 and "Retry-After" in cooldown.headers

    @pytest.mark.parametrize(
        ("case", "status"),
        [
            ("bad_signature", 400),
            ("sha_mismatch", 400),
            ("wrong_hotkey_in_pack", 400),
            ("too_few_cases", 400),
            ("unregistered", 400),
            ("not_json", 400),
        ],
    )
    async def test_upload_rejections(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        case: str,
        status: int,
    ) -> None:
        _install(
            app,
            session_maker,
            registered=(_OTHER_MINER,) if case == "unregistered" else (_MINER,),
        )
        await _open_lane(client)
        raw = _pack_bytes(_MINER.ss58_address)
        body = _upload_body(_MINER, raw)
        if case == "bad_signature":
            body["signature"] = "00" * 64
        elif case == "sha_mismatch":
            body = _upload_body(_MINER, raw, sha="ab" * 32)
        elif case == "wrong_hotkey_in_pack":
            body = _upload_body(_MINER, _pack_bytes(_OTHER_MINER.ss58_address))
        elif case == "too_few_cases":
            body = _upload_body(_MINER, _pack_bytes(_MINER.ss58_address, cases=3))
        elif case == "not_json":
            body = _upload_body(_MINER, b"\xff\xfe not json")
        resp = await client.post("/api/v1/generator/packs", json=body)
        assert resp.status_code == status, resp.text
        async with session_maker() as session:
            assert (
                await session.scalar(select(func.count()).select_from(GeneratorPack))
            ) == 0


class TestScorer:
    async def test_scorer_endpoints_require_the_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        assert (await client.get("/api/v1/generator-scorer/work")).status_code == 401
        bad = {"Authorization": "Bearer nope"}
        assert (
            await client.get("/api/v1/generator-scorer/work", headers=bad)
        ).status_code == 401
        app.state.config = replace(app.state.config, generator_scorer_token=None)
        assert (
            await client.get("/api/v1/generator-scorer/work", headers=_SCORER)
        ).status_code == 503

    async def test_full_loop_scores_and_publishes(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        fixed_panel: list[dict[str, Any]],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client)
        assert (
            await client.get("/api/v1/generator-scorer/work", headers=_SCORER)
        ).json()["work"] is None
        raw = _pack_bytes(_MINER.ss58_address)
        accepted = await _upload(client, raw)

        work = await _lease(client)
        assert work["pack_id"] == accepted["pack_id"]
        assert base64.b64decode(work["pack_base64"]) == raw
        assert [slot["agent_id"] for slot in work["panel"]] == [
            s["agent_id"] for s in fixed_panel
        ]
        # A second scorer gets nothing while the lease is live.
        assert (
            await client.get("/api/v1/generator-scorer/work", headers=_SCORER)
        ).json()["work"] is None

        refs = _refs(raw)
        # Champion fails everything; b and c solve; d half-solves.
        scores = {"agent-champ": 0.0, "agent-b": 1.0, "agent-c": 1.0, "agent-d": 0.25}
        panel = _results(refs, scores, agents=list(scores)) + _results(
            refs, {"intruder": 1.0}, agents=["intruder"]
        )
        result = {
            "lease_id": work["lease_id"],
            "pack_id": work["pack_id"],
            "pack_sha256": work["pack_sha256"],
            "scorer_version": "challenge-scorer/1",
            "validation": {
                "valid": True,
                "pack_sha256": work["pack_sha256"],
                "case_count": 8,
                "cases": refs,
                "issues": [],
            },
            "panel": panel,
        }
        resp = await client.post(
            "/api/v1/generator-scorer/results", headers=_SCORER, json=result
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "scored"
        # mean = (0 + 1 + 1 + 0.25)/4 = 0.5625; credit = 0.4375 * 1.5 per case.
        expected = 8 * (0.4375 * 1.5) / (1.5 * 8)
        assert body["score"] == pytest.approx(expected)
        replay = await client.post(
            "/api/v1/generator-scorer/results", headers=_SCORER, json=result
        )
        assert replay.status_code == 409

        async with session_maker() as session:
            verdicts = list(
                await session.scalars(
                    select(GeneratorCaseVerdict).where(
                        GeneratorCaseVerdict.pack_id == accepted["pack_id"]
                    )
                )
            )
            assert len(verdicts) == 8
            assert {v.status for v in verdicts} == {"discriminating"}
            assert all(v.panel_size == 4 and v.champion_failed for v in verdicts)

        board = (await client.get("/api/v1/generator/board")).json()
        assert board["mode"] == "shadow" and board["count"] == 1
        entry = board["entries"][0]
        assert entry["miner_hotkey"] == _MINER.ss58_address
        assert entry["weight_eligible"] is False
        assert entry["score"] == pytest.approx(expected)

        detail = await client.get(
            f"/api/v1/admin/generator-competition/packs/{accepted['pack_id']}",
            headers=_ADMIN,
        )
        assert detail.status_code == 200
        assert len(detail.json()["verdicts"]) == 8
        assert "pack_base64" not in detail.text
        candidates = await client.get(
            "/api/v1/admin/generator-competition/promotion-candidates", headers=_ADMIN
        )
        assert len(candidates.json()["candidates"]) == 8

        # A second miner copying the same cases earns nothing for them.
        copy = _pack_bytes(_OTHER_MINER.ss58_address)
        await _upload(client, copy, key=_OTHER_MINER)
        work2 = await _lease(client)
        result2 = {
            **result,
            "lease_id": work2["lease_id"],
            "pack_id": work2["pack_id"],
            "pack_sha256": work2["pack_sha256"],
        }
        result2["validation"] = {
            **result["validation"],
            "pack_sha256": work2["pack_sha256"],
            "cases": _refs(copy),
        }
        resp2 = await client.post(
            "/api/v1/generator-scorer/results", headers=_SCORER, json=result2
        )
        assert resp2.status_code == 200 and resp2.json()["score"] == 0.0
        listing = await client.get(
            "/api/v1/generator/packs", params={"hotkey": _OTHER_MINER.ss58_address}
        )
        assert listing.json()["packs"][0]["case_counts"]["duplicate"] == 8

    async def test_invalid_pack_and_insufficient_panel(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        fixed_panel: list[dict[str, Any]],  # noqa: ARG002 - pins the panel
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client, upload_cooldown_seconds=60)
        raw = _pack_bytes(_MINER.ss58_address)
        accepted = await _upload(client, raw)
        work = await _lease(client)
        refs = _refs(raw)
        base = {
            "lease_id": work["lease_id"],
            "pack_id": work["pack_id"],
            "pack_sha256": work["pack_sha256"],
            "scorer_version": "challenge-scorer/1",
        }
        # Only two panel agents had harnesses: below min_panel, so re-queued.
        short = {
            **base,
            "validation": {"valid": True, "case_count": 8, "cases": refs, "issues": []},
            "panel": _results(
                refs, {"agent-b": 1.0, "agent-c": 0.0}, agents=["agent-b", "agent-c"]
            ),
        }
        resp = await client.post(
            "/api/v1/generator-scorer/results", headers=_SCORER, json=short
        )
        assert resp.json() == {
            "pack_id": accepted["pack_id"],
            "status": "queued",
            "outcome": "insufficient_panel",
            "score": None,
        }
        async with session_maker() as session:
            row = await session.get(GeneratorPack, accepted["pack_id"])
            assert row is not None and row.status == "queued" and row.lease_id is None
            row.not_before = row.submitted_at  # skip the backoff
            await session.commit()

        work = await _lease(client)
        invalid = {
            **base,
            "lease_id": work["lease_id"],
            "validation": {
                "valid": False,
                "issues": [
                    {"case_id": "c0", "rule": "grounding", "message": "missing"}
                ],
            },
        }
        resp = await client.post(
            "/api/v1/generator-scorer/results", headers=_SCORER, json=invalid
        )
        assert resp.json()["status"] == "invalid"
        listing = await client.get(
            "/api/v1/generator/packs", params={"hotkey": _MINER.ss58_address}
        )
        assert listing.json()["packs"][0]["issue_count"] == 1

    async def test_validator_ledger_is_shadow(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        _install_chain(app)
        resp = await client.get(
            "/api/v1/scoring/generator-ledger", headers=_ledger_headers()
        )
        assert resp.status_code == 200
        assert resp.json()["entries"] == [] and resp.json()["mode"] == "off"
        assert (await client.get("/api/v1/scoring/generator-ledger")).status_code == 401
