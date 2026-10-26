"""Contract tests for the shadow generator competition lane.

Covers the operator settings board, signed source uploads (archive checks,
duplicate trees, supersession), the trusted scorer's qualification loop (fresh
secret seeds, generator failures, nondeterminism, behavioral copies on the
probe seed, expired leases), the validator ledger, and the operator actions.
Rounds, where qualified generators are scored against agents, are covered by
``test_generator_rounds.py``; archive inspection by
``ditto/tests/test_generator_archive.py``.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import tarfile
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.generator import (
    PROBE_ROTATION_CONFIRMATION,
    REJECT_SUBMISSION_CONFIRMATION,
    generator_settings_confirmation,
    source_signature_payload,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.chain.models import NeuronInfo
from ditto.db.models import (
    GeneratorOperatorAction,
    GeneratorProbeEpoch,
    GeneratorRun,
    GeneratorSubmission,
)
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
        "seeds_per_run": 2,
        "cases_per_seed": 8,
        "window_days": 7,
        "copy_overlap_threshold": 0.5,
        "upload_cooldown_seconds": 60,
        "lease_ttl_seconds": 600,
        "max_attempts": 2,
        "retry_backoff_seconds": 60,
        "epochs_per_round": 4,
        "counted_columns": 5,
        "challenger_columns": 1,
        "reference_agent_ids": [],
        "privacy_window_rounds": 6,
        "onchain_commitment": False,
        "validator_rounds_enabled": False,
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


def _archive(salt: str = "", *, files: dict[str, bytes] | None = None) -> bytes:
    tree = files or {
        "go.mod": b"module example.com/gen\n\ngo 1.23\n",
        "main.go": f"package main\n\n// {salt}\nfunc main() {{}}\n".encode(),
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in sorted(tree):
            info = tarfile.TarInfo(name)
            info.size = len(tree[name])
            tar.addfile(info, io.BytesIO(tree[name]))
    return gzip.compress(buf.getvalue(), mtime=0)


def _upload_body(
    key: bittensor.Keypair, raw: bytes, *, sha: str | None = None
) -> dict[str, Any]:
    digest = sha or hashlib.sha256(raw).hexdigest()
    nonce = uuid4()
    ts = int(time.time())
    signature = key.sign(
        source_signature_payload(
            hotkey=key.ss58_address, source_sha256=digest, timestamp=ts, nonce=nonce
        )
    ).hex()
    return {
        "hotkey": key.ss58_address,
        "source_sha256": digest,
        "source_base64": base64.b64encode(raw).decode(),
        "signature": signature,
        "signature_timestamp": ts,
        "signature_nonce": str(nonce),
    }


def _refs(tag: str, count: int = 8) -> list[dict[str, str]]:
    """Case refs a generator would emit: fingerprints derived from ``tag``."""
    return [
        {
            "case_id": f"c{i}",
            "fingerprint": hashlib.sha256(f"{tag}:{i}".encode()).hexdigest(),
        }
        for i in range(count)
    ]


def _seed_output(seed: str, refs: list[dict[str, str]], **extra: Any) -> dict[str, Any]:
    return {
        "seed": seed,
        "pack_sha256": hashlib.sha256(seed.encode()).hexdigest(),
        "validation": {"valid": True, "case_count": len(refs), "cases": refs},
        "duration_ms": 12,
        **extra,
    }


def _result(
    work: dict[str, Any],
    *,
    tag: str,
    probe_tag: str | None = None,
    deterministic: bool | None = True,
    build_ok: bool = True,
    seed_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    seeds = []
    if build_ok:
        for index, seed in enumerate(work["seeds"]):
            refs = _refs(f"{tag}:{index}", work["cases_per_seed"])
            out = _seed_output(seed, refs)
            if index == 0 and seed_overrides:
                out.update(seed_overrides)
            seeds.append(out)
    probe = (
        _seed_output(work["probe_seed"], _refs(f"probe:{probe_tag or tag}"))
        if build_ok
        else None
    )
    return {
        "lease_id": work["lease_id"],
        "run_id": work["run_id"],
        "submission_id": work["submission_id"],
        "source_sha256": work["source_sha256"],
        "scorer_version": "challenge-scorer/3",
        "sandbox": "docker",
        "build": {"ok": build_ok, "error": None if build_ok else "compile_failed: x"},
        "deterministic": deterministic if build_ok else None,
        "seeds": seeds,
        "probe": probe,
    }


async def _upload(client: httpx.AsyncClient, raw: bytes, key=_MINER) -> dict:
    resp = await client.post(
        "/api/v1/generator/submissions", json=_upload_body(key, raw)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _lease(client: httpx.AsyncClient) -> dict:
    resp = await client.get("/api/v1/generator-scorer/work", headers=_SCORER)
    assert resp.status_code == 200, resp.text
    return resp.json()["work"]


async def _post(client: httpx.AsyncClient, result: dict[str, Any]) -> httpx.Response:
    return await client.post(
        "/api/v1/generator-scorer/results", headers=_SCORER, json=result
    )


async def _make_due(
    maker: async_sessionmaker[AsyncSession], submission_id: str
) -> None:
    async with maker() as session:
        row = await session.get(GeneratorSubmission, UUID(submission_id))
        assert row is not None
        row.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()


async def _skip_cooldown(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as session:
        for row in await session.scalars(select(GeneratorSubmission)):
            row.submitted_at = row.submitted_at - timedelta(hours=2)
        await session.commit()


def test_signature_payload_is_the_pinned_wire_format() -> None:
    """Pinned literally here and in the miner CLI's ``test_generator``."""
    nonce = UUID("4b0d7e8e-5f3c-4c4e-9a8b-1e2f3a4b5c6d")
    payload = source_signature_payload(
        hotkey=_MINER.ss58_address,
        source_sha256="ab" * 32,
        timestamp=1_700_000_000,
        nonce=nonce,
    )
    expected = f"ditto-generator-v1:{_MINER.ss58_address}:{'ab' * 32}"
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

    async def test_enforce_needs_its_own_confirmation(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        assert generator_settings_confirmation("enforce") == (
            "APPLY GENERATOR COMPETITION ENFORCE"
        )
        body = {
            "expected_revision": 0,
            "settings": _settings(mode="enforce"),
            "reason": "mark qualified generators weight-eligible",
            "confirmation": generator_settings_confirmation("shadow"),
        }
        refused = await client.post(_SETTINGS_URL, headers=_ADMIN, json=body)
        assert refused.status_code == 409
        assert "APPLY GENERATOR COMPETITION ENFORCE" in refused.text
        body["confirmation"] = generator_settings_confirmation("enforce")
        ok = await client.post(_SETTINGS_URL, headers=_ADMIN, json=body)
        assert ok.status_code == 200, ok.text
        board = (await client.get(_SETTINGS_URL, headers=_ADMIN)).json()
        assert board["effective"]["settings"]["mode"] == "enforce"


class TestUpload:
    async def test_closed_lane_refuses_uploads(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        resp = await client.post(
            "/api/v1/generator/submissions", json=_upload_body(_MINER, _archive())
        )
        assert resp.status_code == 403

    async def test_signed_upload_is_stored_privately_and_supersedes(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client)
        raw = _archive("one")
        accepted = await _upload(client, raw)
        assert accepted["status"] == "active" and accepted["superseded"] == []
        async with session_maker() as session:
            row = await session.get(
                GeneratorSubmission, UUID(accepted["submission_id"])
            )
            assert row is not None and row.source_bytes == raw
            assert row.owner_key.startswith("coldkey:")
            assert row.module == "example.com/gen" and row.file_count == 2
        listing = await client.get(
            "/api/v1/generator/submissions", params={"hotkey": _MINER.ss58_address}
        )
        item = listing.json()["submissions"][0]
        assert item["source_sha256"] == hashlib.sha256(raw).hexdigest()
        assert "source_base64" not in listing.text and item["latest_run"] is None

        dup = await client.post(
            "/api/v1/generator/submissions", json=_upload_body(_MINER, raw)
        )
        assert dup.status_code == 409
        cooldown = await client.post(
            "/api/v1/generator/submissions", json=_upload_body(_MINER, _archive("two"))
        )
        assert cooldown.status_code == 429 and "Retry-After" in cooldown.headers

        await _skip_cooldown(session_maker)
        second = await _upload(client, _archive("two"))
        assert second["superseded"] == [accepted["submission_id"]]
        async with session_maker() as session:
            first = await session.get(
                GeneratorSubmission, UUID(accepted["submission_id"])
            )
            assert first is not None and first.status == "superseded"

    async def test_repacked_tree_is_a_duplicate_for_anyone(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client)
        files = {
            "go.mod": b"module example.com/gen\n\ngo 1.23\n",
            "main.go": b"package main\nfunc main(){}\n",
        }
        await _upload(client, _archive(files=files))
        # Same files, different archive bytes (an extra root dir entry).
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            root = tarfile.TarInfo("./")
            root.type = tarfile.DIRTYPE
            tar.addfile(root)
            for name, data in files.items():
                info = tarfile.TarInfo(f"./{name}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        repacked = gzip.compress(buf.getvalue(), mtime=0)
        resp = await client.post(
            "/api/v1/generator/submissions", json=_upload_body(_OTHER_MINER, repacked)
        )
        assert resp.status_code == 409 and "source files" in resp.text

    @pytest.mark.parametrize(
        ("case", "status"),
        [
            ("bad_signature", 400),
            ("sha_mismatch", 400),
            ("not_gzip", 400),
            ("no_go_mod", 400),
            ("unvendored", 400),
            ("unregistered", 400),
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
        raw = _archive()
        body = _upload_body(_MINER, raw)
        if case == "bad_signature":
            body["signature"] = "00" * 64
        elif case == "sha_mismatch":
            body = _upload_body(_MINER, raw, sha="ab" * 32)
        elif case == "not_gzip":
            body = _upload_body(_MINER, b"plain bytes")
        elif case == "no_go_mod":
            body = _upload_body(_MINER, _archive(files={"main.go": b"package main\n"}))
        elif case == "unvendored":
            body = _upload_body(
                _MINER,
                _archive(
                    files={
                        "go.mod": b"module x\n\ngo 1.23\n\nrequire y v1.0.0\n",
                        "main.go": b"package main\n",
                    }
                ),
            )
        resp = await client.post("/api/v1/generator/submissions", json=body)
        assert resp.status_code == status, resp.text
        async with session_maker() as session:
            count = await session.scalar(
                select(func.count()).select_from(GeneratorSubmission)
            )
            assert count == 0


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

    async def test_qualification_loop(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client)
        assert (await _lease(client)) is None
        raw = _archive("gen")
        accepted = await _upload(client, raw)

        work = await _lease(client)
        assert work["submission_id"] == accepted["submission_id"]
        assert base64.b64decode(work["source_base64"]) == raw
        assert len(work["seeds"]) == 2 and len(set(work["seeds"])) == 2
        assert all(len(seed) == 64 for seed in [*work["seeds"], work["probe_seed"]])
        assert work["probe_seed"] not in work["seeds"] and work["cases_per_seed"] == 8
        assert "panel" not in work
        # A second scorer gets nothing while the lease is live.
        assert (await _lease(client)) is None

        result = _result(work, tag="gen")
        resp = await _post(client, result)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "qualified" and body["submission_status"] == "active"
        assert body["score"] is None
        assert (await _post(client, result)).status_code == 409  # replay
        async with session_maker() as session:
            row = await session.get(
                GeneratorSubmission, UUID(accepted["submission_id"])
            )
            assert row is not None and row.lease_id is None
            assert row.qualified_at is not None
        # A qualified generator is not leased again: rounds take over.
        assert (await _lease(client)) is None

        run_detail = await client.get(
            f"/api/v1/admin/generator-competition/runs/{work['run_id']}",
            headers=_ADMIN,
        )
        assert run_detail.status_code == 200
        detail = run_detail.json()
        assert detail["seeds"] == work["seeds"]
        assert detail["evaluation"]["deterministic"] is True
        assert work["probe_seed"] not in run_detail.text
        assert "source_base64" not in run_detail.text
        listing = await client.get(
            "/api/v1/generator/submissions", params={"hotkey": _MINER.ss58_address}
        )
        item = listing.json()["submissions"][0]
        assert item["qualified_at"] is not None and item["next_run_at"] is None
        assert item["latest_run"]["status"] == "qualified"

    @pytest.mark.parametrize(
        ("case", "outcome"),
        [
            ("build_failed", "build_failed"),
            ("crash", "generation_failed"),
            ("invalid_pack", "invalid_pack"),
            ("nondeterministic", "nondeterministic"),
        ],
    )
    async def test_generator_failures_reject_the_submission(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        case: str,
        outcome: str,
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client)
        accepted = await _upload(client, _archive())
        work = await _lease(client)
        if case == "build_failed":
            result = _result(work, tag="x", build_ok=False)
        elif case == "crash":
            result = _result(
                work,
                tag="x",
                deterministic=None,
                seed_overrides={"error": "generator generate: exit: exit 3: boom"},
            )
        elif case == "invalid_pack":
            result = _result(
                work,
                tag="x",
                seed_overrides={
                    "validation": {
                        "valid": False,
                        "issues": [
                            {"case_id": "c0", "rule": "grounding", "message": "m"}
                        ],
                    }
                },
            )
        else:
            result = _result(work, tag="x", deterministic=False)
        resp = await _post(client, result)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "invalid"
        assert resp.json()["outcome"] == outcome
        assert resp.json()["submission_status"] == "rejected"
        async with session_maker() as session:
            row = await session.get(
                GeneratorSubmission, UUID(accepted["submission_id"])
            )
            assert row is not None and row.status_reason is not None
            assert row.status_reason.startswith(outcome)
        assert (await _lease(client)) is None

    async def test_results_must_match_the_lease(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client)
        await _upload(client, _archive())
        work = await _lease(client)
        swapped = _result(work, tag="x")
        swapped["seeds"][0]["seed"] = "ab" * 32  # not a seed Platform issued
        assert (await _post(client, swapped)).status_code == 409
        wrong_probe = _result(work, tag="x")
        wrong_probe["probe"]["seed"] = "cd" * 32
        assert (await _post(client, wrong_probe)).status_code == 409
        unchecked = _result(work, tag="x", deterministic=None)
        assert (await _post(client, unchecked)).status_code == 409
        short = _result(work, tag="x")
        short["seeds"][1]["validation"]["cases"] = short["seeds"][1]["validation"][
            "cases"
        ][:5]
        assert (await _post(client, short)).status_code == 409
        assert (await _post(client, _result(work, tag="x"))).status_code == 200

    async def test_behavioral_copy_on_the_probe_seed_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client)
        original = await _upload(client, _archive("original"))
        work = await _lease(client)
        assert (await _post(client, _result(work, tag="a", probe_tag="same"))).json()[
            "status"
        ] == "qualified"

        # A cosmetically different archive from another owner emits the same
        # cases on the probe seed: same program, rejected.
        copy = await _upload(client, _archive("renamed"), key=_OTHER_MINER)
        work2 = await _lease(client)
        assert work2["probe_seed"] == work["probe_seed"]
        resp = await _post(client, _result(work2, tag="b", probe_tag="same"))
        assert resp.json()["status"] == "copy"
        assert resp.json()["submission_status"] == "rejected"
        async with session_maker() as session:
            row = await session.get(GeneratorSubmission, UUID(copy["submission_id"]))
            assert row is not None and row.status_reason is not None
            assert original["submission_id"] in row.status_reason

        # Rotating the probe seed starts a new comparison epoch.
        rotated = await client.post(
            "/api/v1/admin/generator-competition/probe/rotate",
            headers=_ADMIN,
            json={
                "reason": "suspected probe leak",
                "confirmation": PROBE_ROTATION_CONFIRMATION,
            },
        )
        assert rotated.status_code == 200, rotated.text
        probe = rotated.json()
        assert probe["current"]["epoch"] == 2 and len(probe["history"]) == 2
        assert probe["history"][1]["probed_submissions"] == 2
        assert "seed" not in probe["current"]
        # Rotation re-qualifies every active generator on the new probe.
        async with session_maker() as session:
            row = await session.get(
                GeneratorSubmission, UUID(original["submission_id"])
            )
            assert row is not None and row.qualified_at is None
        async with session_maker() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(GeneratorProbeEpoch)
                    .where(GeneratorProbeEpoch.retired_at.is_(None))
                )
            ) == 1

    async def test_expired_leases_rotate_seeds(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client)
        accepted = await _upload(client, _archive())
        first = await _lease(client)
        async with session_maker() as session:
            row = await session.get(
                GeneratorSubmission, UUID(accepted["submission_id"])
            )
            assert row is not None
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        second = await _lease(client)
        assert second["run_id"] != first["run_id"]
        assert not set(second["seeds"]) & set(first["seeds"])
        assert (await _post(client, _result(first, tag="x"))).status_code == 409
        async with session_maker() as session:
            old = await session.get(GeneratorRun, UUID(first["run_id"]))
            assert old is not None and old.status == "abandoned"

    async def test_a_lapsed_lease_cannot_report(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client)
        accepted = await _upload(client, _archive())
        work = await _lease(client)
        async with session_maker() as session:
            row = await session.get(
                GeneratorSubmission, UUID(accepted["submission_id"])
            )
            assert row is not None
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        resp = await _post(client, _result(work, tag="late"))
        assert resp.status_code == 409 and "expired" in resp.text
        async with session_maker() as session:
            row = await session.get(
                GeneratorSubmission, UUID(accepted["submission_id"])
            )
            assert row is not None and row.qualified_at is None

    async def test_a_run_leased_before_a_probe_rotation_cannot_qualify(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client)
        accepted = await _upload(client, _archive())
        work = await _lease(client)
        rotated = await client.post(
            "/api/v1/admin/generator-competition/probe/rotate",
            headers=_ADMIN,
            json={
                "reason": "rotate while a run is leased",
                "confirmation": PROBE_ROTATION_CONFIRMATION,
            },
        )
        assert rotated.status_code == 200, rotated.text
        resp = await _post(client, _result(work, tag="stale"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "abandoned"
        async with session_maker() as session:
            row = await session.get(
                GeneratorSubmission, UUID(accepted["submission_id"])
            )
            run = await session.get(GeneratorRun, UUID(work["run_id"]))
            assert row is not None and row.qualified_at is None
            assert run is not None and run.outcome == "probe_rotated"
        again = await _lease(client)
        assert again["run_id"] != work["run_id"]
        assert again["probe_seed"] != work["probe_seed"]

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


class TestOperatorActions:
    async def test_a_requalification_during_a_run_survives_its_result(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client)
        accepted = await _upload(client, _archive())
        sub_id = accepted["submission_id"]
        work = await _lease(client)
        resp = await client.post(
            f"/api/v1/admin/generator-competition/submissions/{sub_id}/rescore",
            headers=_ADMIN,
            json={"expected_status": "active", "reason": "re-check after a fix"},
        )
        assert resp.status_code == 200, resp.text
        assert (await _post(client, _result(work, tag="r"))).json()[
            "status"
        ] == "qualified"
        async with session_maker() as session:
            row = await session.get(GeneratorSubmission, UUID(sub_id))
            assert row is not None and row.qualified_at is None
        rerun = await _lease(client)
        assert rerun is not None and rerun["run_id"] != work["run_id"]

    async def test_requalify_and_reject(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client)
        accepted = await _upload(client, _archive())
        sub_id = accepted["submission_id"]
        base = f"/api/v1/admin/generator-competition/submissions/{sub_id}"
        work = await _lease(client)
        assert (await _post(client, _result(work, tag="r"))).json()[
            "status"
        ] == "qualified"
        assert (await _lease(client)) is None  # qualified generators idle here

        body = {"expected_status": "active", "reason": "re-check after a fix"}
        assert (await client.post(f"{base}/rescore", json=body)).status_code == 401
        stale = {**body, "expected_status": "rejected"}
        assert (
            await client.post(f"{base}/rescore", headers=_ADMIN, json=stale)
        ).status_code == 409
        resp = await client.post(f"{base}/rescore", headers=_ADMIN, json=body)
        assert resp.status_code == 200, resp.text
        async with session_maker() as session:
            audit = (
                await session.scalars(
                    select(GeneratorOperatorAction).where(
                        GeneratorOperatorAction.subject == sub_id
                    )
                )
            ).all()
            assert [a.action for a in audit] == ["rescore"]
            assert audit[0].reason == body["reason"]
        rerun = await _lease(client)
        assert rerun is not None and rerun["submission_id"] == sub_id

        reject = {
            "expected_status": "active",
            "reason": "copy of another miner's generator",
        }
        assert (
            await client.post(f"{base}/reject", headers=_ADMIN, json=reject)
        ).status_code == 409  # no confirmation
        reject["confirmation"] = REJECT_SUBMISSION_CONFIRMATION
        resp = await client.post(f"{base}/reject", headers=_ADMIN, json=reject)
        assert resp.status_code == 200, resp.text
        detail = resp.json()
        assert detail["summary"]["status"] == "rejected"
        assert detail["summary"]["status_reason"].startswith("operator (")
        assert detail["lease_run_id"] is None
        assert [r["status"] for r in detail["runs"]] == ["abandoned", "qualified"]
        assert detail["summary"]["qualified_at"] is None
        # The in-flight run can no longer report.
        assert (await _post(client, _result(rerun, tag="r2"))).status_code == 409
        assert (
            await client.post(
                f"{base}/reject",
                headers=_ADMIN,
                json={**reject, "expected_status": "rejected"},
            )
        ).status_code == 409
