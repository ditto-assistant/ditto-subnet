"""Contract tests for the model_context (CPM) competition lane.

Covers docs/research/cpm/competition-v1.md sections 3 and 5 against a real
Postgres: guarded settings and the ``mode=off`` default, signed and committed
submissions, asynchronous screening with a fake Hugging Face fetcher (hash
checks, rank cap, duplicate weights by commitment block), seasons and seeds,
the scorer's lease / result / fault lifecycle, and the ledger and board.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.model_context import (
    MAX_FAULT_DETAIL_CHARS,
    MAX_FAULT_REQUEST_DETAIL_CHARS,
    REJECT_SUBMISSION_CONFIRMATION,
    RESCORE_SUBMISSION_CONFIRMATION,
    ROTATE_SEASON_CONFIRMATION,
    ModelContextSettings,
    commitment_value,
    generate_seed,
    model_context_settings_confirmation,
    season_seeds,
    seed_score,
    signature_payload,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.model_context_competition import (
    MAX_SCREENING_ATTEMPTS,
    rotate_season,
    screen_next,
)
from ditto.chain.errors import ChainConnectionError
from ditto.chain.models import BlockInfo, HotkeyCommitment, NeuronInfo
from ditto.db.models import (
    ModelContextOperatorAction,
    ModelContextRun,
    ModelContextSubmission,
)
from ditto.tests.api_server.conftest import answer_registration_from_neurons
from ditto.tests.api_server.endpoints.test_scoring import (
    _KEYPAIR as _VALIDATOR,
)
from ditto.tests.api_server.endpoints.test_scoring import _ledger_headers
from ditto.tests.model_context_fixtures import FakeFetcher, lora_repo

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_SCORER_TOKEN = "test-model-context-scorer-token-32-chars"
_ADMIN = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_SCORER = {"Authorization": f"Bearer {_SCORER_TOKEN}"}
_MINER = bittensor.Keypair.create_from_uri("//Bob")
_OTHER = bittensor.Keypair.create_from_uri("//Charlie")
_STRANGER = bittensor.Keypair.create_from_uri("//Dave")
_SETTINGS_URL = "/api/v1/admin/model-context/settings"
_SUBMIT_URL = "/api/v1/model-context/submissions"
_WORK_URL = "/api/v1/model-context-scorer/work?scorer_id=cpm-1&gpu_sku=L40S"
_BLOCK_HASH = "0x" + "cd" * 32


class _Chain:
    """A fake chain: registered neurons, commitments, and the finalized head."""

    def __init__(self) -> None:
        self.commitments: dict[str, tuple[int, str]] = {}
        self.block = BlockInfo(number=5_000_000, hash=_BLOCK_HASH)
        self.mock = MagicMock()
        self.mock.get_recent_neurons = AsyncMock(
            return_value=[
                NeuronInfo(
                    hotkey=_VALIDATOR.ss58_address,
                    coldkey="5ValidatorColdkeyPlaceholderXXXXXXXXXXXXXXXXXXXX",
                    uid=0,
                    stake=1000.0,
                    validator_permit=True,
                ),
                *[
                    NeuronInfo(
                        hotkey=key.ss58_address,
                        coldkey=f"5MinerColdkeyPlaceholderXXXXXXXXXXXXXXXXXXXXX{i:02d}",
                        uid=i,
                        stake=0.0,
                        validator_permit=False,
                    )
                    for i, key in enumerate((_MINER, _OTHER), start=1)
                ],
            ]
        )
        answer_registration_from_neurons(self.mock)
        self.mock.get_hotkey_commitment = AsyncMock(side_effect=self._commitment)
        self.mock.get_finalized_block = AsyncMock(side_effect=self._block)

    async def _commitment(self, hotkey: str, netuid: int) -> HotkeyCommitment | None:
        found = self.commitments.get(hotkey)
        if found is None:
            return None
        block, value = found
        return HotkeyCommitment(
            hotkey=hotkey,
            netuid=netuid,
            block=block,
            data=value.encode(),
            read_at_block=self.block.number,
        )

    async def _block(self) -> BlockInfo:
        return self.block


@pytest.fixture
def chain(app: FastAPI, session_maker: async_sessionmaker[AsyncSession]) -> _Chain:
    fake = _Chain()
    app.state.config = replace(
        app.state.config,
        admin_api_token=_ADMIN_TOKEN,
        model_context_scorer_token=_SCORER_TOKEN,
    )
    app.state.session_maker = session_maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    async def _chain() -> MagicMock:
        return fake.mock

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_chain_client] = _chain
    return fake


def _settings(**overrides: Any) -> dict[str, Any]:
    base = ModelContextSettings(mode="shadow").model_dump(mode="json")
    base["upload_cooldown_seconds"] = 60
    base.update(overrides)
    return base


async def _open_lane(client: httpx.AsyncClient, **overrides: Any) -> None:
    current = (await client.get(_SETTINGS_URL, headers=_ADMIN)).json()
    settings = _settings(**overrides)
    resp = await client.post(
        _SETTINGS_URL,
        headers=_ADMIN,
        json={
            "expected_revision": current["effective"]["revision"],
            "settings": settings,
            "reason": "open the shadow model_context lane",
            "actor": "backroom:test",
            "confirmation": model_context_settings_confirmation(settings["mode"]),
        },
    )
    assert resp.status_code == 200, resp.text


def _revision(tag: str) -> str:
    return hashlib.sha1(tag.encode()).hexdigest()


def _body(
    key: bittensor.Keypair,
    manifest: str,
    *,
    repo: str = "miner/cpm",
    revision: str | None = None,
    ts: int | None = None,
    signer: bittensor.Keypair | None = None,
) -> dict[str, Any]:
    revision = revision or _revision(manifest)
    ts = ts or int(time.time())
    nonce = uuid4()
    digest = hashlib.sha256(manifest.encode()).hexdigest()
    signature = (signer or key).sign(
        signature_payload(
            hotkey=key.ss58_address,
            hf_repo=repo,
            revision=revision,
            manifest_sha256=digest,
            ts=ts,
            nonce=nonce,
        )
    )
    return {
        "hotkey": key.ss58_address,
        "hf_repo": repo,
        "revision": revision,
        "manifest": manifest,
        "signature": signature.hex(),
        "ts": ts,
        "nonce": str(nonce),
    }


def _commit(chain: _Chain, key: bittensor.Keypair, manifest: str, block: int) -> None:
    chain.commitments[key.ss58_address] = (
        block,
        commitment_value(hashlib.sha256(manifest.encode()).hexdigest()),
    )


async def _submit(
    client: httpx.AsyncClient,
    chain: _Chain,
    fetcher: FakeFetcher,
    key: bittensor.Keypair,
    *,
    block: int,
    repo: str = "miner/cpm",
    **repo_kwargs: Any,
) -> dict[str, Any]:
    manifest, files = lora_repo(**repo_kwargs)
    body = _body(key, manifest, repo=repo)
    fetcher.put(
        repo, body["revision"], {"cpm-manifest.json": manifest.encode(), **files}
    )
    _commit(chain, key, manifest, block)
    resp = await client.post(_SUBMIT_URL, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _skip_cooldown(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as session:
        await session.execute(
            update(ModelContextSubmission).values(
                created_at=ModelContextSubmission.created_at - timedelta(hours=7)
            )
        )
        await session.commit()


async def _screen_all(
    maker: async_sessionmaker[AsyncSession], fetcher: FakeFetcher
) -> list[str]:
    outcomes = []
    while (outcome := await screen_next(maker, fetcher)) is not None:
        outcomes.append(outcome[1])
    return outcomes


async def _status(maker: async_sessionmaker[AsyncSession], submission_id: str) -> Any:
    async with maker() as session:
        return await session.get(ModelContextSubmission, UUID(submission_id))


async def _open_season(maker: async_sessionmaker[AsyncSession], chain: _Chain) -> int:
    number = await rotate_season(
        maker, read_block=chain.mock.get_finalized_block, force=False
    )
    assert number is not None
    return number


def _result(
    work: dict[str, Any], *, quality: float, int64: bool = False
) -> dict[str, Any]:
    per_seed = []
    for seed in work["generate_seeds" if int64 else "seeds"]:
        memory, distractor, tool = quality, 0.2, quality / 2
        per_seed.append(
            {
                "seed": seed,
                "score": seed_score(memory, distractor, tool),
                "memory_recall": memory,
                "distractor_rate": distractor,
                "tool_recall": tool,
                "memory_cases": 40,
                "tool_cases": 30,
                "packed_tokens": 81_000,
            }
        )
    mean = sum(s["score"] for s in per_seed) / len(per_seed)
    return {
        "run_id": work["run_id"],
        "lease_id": work["lease_id"],
        "cpm_score_version": 1,
        "score": mean,
        "components": {
            "memory_recall": quality,
            "distractor_rate": 0.2,
            "tool_recall": quality / 2,
            "memory_cases": 120,
            "tool_cases": 90,
        },
        "per_seed": per_seed,
        "scorer_id": "cpm-1",
        "scorer_version": "0.1.0",
        "gpu_sku": "L40S",
    }


async def _score_all(
    client: httpx.AsyncClient, qualities: dict[str, float]
) -> list[dict[str, Any]]:
    """Lease until 204, scoring each run with the quality for its subject."""
    done: list[dict[str, Any]] = []
    while True:
        resp = await client.get(_WORK_URL, headers=_SCORER)
        if resp.status_code == 204:
            return done
        assert resp.status_code == 200, resp.text
        work = resp.json()
        subject = (
            work["submission"]["hotkey"]
            if work["kind"] == "submission"
            else work["base"]["base_id"]
        )
        posted = await client.post(
            "/api/v1/model-context-scorer/results",
            headers=_SCORER,
            json=_result(work, quality=qualities[subject]),
        )
        assert posted.status_code == 200, posted.text
        done.append(work)


async def _score_references(client: httpx.AsyncClient, *, quality: float) -> None:
    """Score the season's two reference runs, which always lease first."""
    for _ in range(2):
        work = (await client.get(_WORK_URL, headers=_SCORER)).json()
        assert work["kind"] == "reference"
        posted = await client.post(
            "/api/v1/model-context-scorer/results",
            headers=_SCORER,
            json=_result(work, quality=quality),
        )
        assert posted.status_code == 200, posted.text


class TestSettings:
    async def test_lane_ships_off_and_refuses_everything(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        assert (await client.get(_SETTINGS_URL)).status_code == 401
        board = (await client.get(_SETTINGS_URL, headers=_ADMIN)).json()
        assert board["effective"]["settings"]["mode"] == "off"
        assert board["effective"]["source"] == "default"
        defaults = board["default"]
        assert defaults["max_lora_rank"] == 64
        assert defaults["max_total_bytes"] == 6 << 30
        assert defaults["upload_cooldown_seconds"] == 21_600
        assert defaults["season_days"] == 7
        assert defaults["floor_margin"] == 0.01
        assert defaults["budget_tokens"] == 2_048
        assert defaults["tool_k"] == 8
        assert defaults["rank_shares"] == [0.5, 0.2, 0.13, 0.1, 0.07]
        assert defaults["upload_fee_required"] is False
        assert [b["base_id"] for b in defaults["base_allowlist"]] == [
            "qwen3.5-0.8b",
            "qwen3.5-2b",
        ]

        manifest, _ = lora_repo()
        _commit(chain, _MINER, manifest, 100)
        refused = await client.post(_SUBMIT_URL, json=_body(_MINER, manifest))
        assert refused.status_code == 403
        terms = (
            await client.get(
                "/api/v1/model-context/upload-terms",
                params={"hotkey": _MINER.ss58_address},
            )
        ).json()
        assert terms["accepting"] is False and terms["blockers"]
        assert terms["commitment_format"] == "dcpm1:{manifest_sha256}"
        assert (await client.get(_WORK_URL, headers=_SCORER)).status_code == 204
        ledger = await client.get(
            "/api/v1/scoring/model-context-ledger", headers=_ledger_headers()
        )
        assert ledger.status_code == 200
        assert ledger.json()["entries"] == [] and ledger.json()["mode"] == "off"
        assert (
            await rotate_season(
                session_maker, read_block=chain.mock.get_finalized_block, force=True
            )
            is None
        )

    @pytest.mark.usefixtures("chain")
    async def test_writes_are_guarded(self, client: httpx.AsyncClient) -> None:
        body = {
            "expected_revision": 0,
            "settings": _settings(),
            "reason": "open the shadow model_context lane",
            "confirmation": "yes",
        }
        assert (
            await client.post(_SETTINGS_URL, headers=_ADMIN, json=body)
        ).status_code == 409
        body["confirmation"] = model_context_settings_confirmation("shadow")
        partial = {**body, "settings": {"mode": "shadow"}}
        assert (
            await client.post(_SETTINGS_URL, headers=_ADMIN, json=partial)
        ).status_code == 422
        fee = {**body, "settings": _settings(upload_fee_required=True)}
        assert (
            await client.post(_SETTINGS_URL, headers=_ADMIN, json=fee)
        ).status_code == 422
        rising = {**body, "settings": _settings(rank_shares=[0.1, 0.5])}
        assert (
            await client.post(_SETTINGS_URL, headers=_ADMIN, json=rising)
        ).status_code == 422
        stale = {**body, "expected_revision": 4}
        assert (
            await client.post(_SETTINGS_URL, headers=_ADMIN, json=stale)
        ).status_code == 409
        ok = await client.post(_SETTINGS_URL, headers=_ADMIN, json=body)
        assert ok.status_code == 200 and ok.json()["revision"] >= 1


class TestSubmission:
    async def test_signed_committed_submission_is_screened_and_queued(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _open_lane(client)
        fetcher = FakeFetcher()
        accepted = await _submit(client, chain, fetcher, _MINER, block=1_000)
        assert accepted["status"] == "screening"
        assert accepted["commitment_block"] == 1_000
        listed = (
            await client.get(_SUBMIT_URL, params={"hotkey": _MINER.ss58_address})
        ).json()["submissions"]
        assert [s["status"] for s in listed] == ["screening"]

        assert await _screen_all(session_maker, fetcher) == ["queued"]
        row = await _status(session_maker, accepted["submission_id"])
        assert row.status == "queued"
        assert row.weights_sha256 is not None and len(row.weights_sha256) == 64
        assert row.screening_detail["rank"] == 4
        # Every file was fetched at the pinned revision, manifest first.
        assert fetcher.requests[0][2] == "cpm-manifest.json"
        assert {r[1] for r in fetcher.requests} == {listed[0]["revision"]}

    async def test_signature_and_commitment_are_required(
        self, client: httpx.AsyncClient, chain: _Chain
    ) -> None:
        await _open_lane(client)
        manifest, _ = lora_repo()
        forged = _body(_MINER, manifest, signer=_OTHER)
        _commit(chain, _MINER, manifest, 100)
        assert (await client.post(_SUBMIT_URL, json=forged)).status_code == 400
        stale = _body(_MINER, manifest, ts=int(time.time()) - 3_600)
        assert (await client.post(_SUBMIT_URL, json=stale)).status_code == 400

        chain.commitments.clear()
        missing = await client.post(_SUBMIT_URL, json=_body(_MINER, manifest))
        assert missing.status_code == 409 and "no commitment" in missing.text
        other_manifest, _ = lora_repo(rank=8)
        _commit(chain, _MINER, other_manifest, 100)
        mismatch = await client.post(_SUBMIT_URL, json=_body(_MINER, manifest))
        assert mismatch.status_code == 409 and "dcpm1:" in mismatch.text

        chain.mock.get_hotkey_commitment.side_effect = ChainConnectionError("down")
        outage = await client.post(_SUBMIT_URL, json=_body(_MINER, manifest))
        assert outage.status_code == 503

        unregistered = await client.post(_SUBMIT_URL, json=_body(_STRANGER, manifest))
        assert unregistered.status_code == 400

    async def test_bad_manifest_and_branch_revision_are_refused(
        self, client: httpx.AsyncClient, chain: _Chain
    ) -> None:
        await _open_lane(client)
        manifest, _ = lora_repo(license="cc-by-nc-4.0")
        _commit(chain, _MINER, manifest, 100)
        bad = await client.post(_SUBMIT_URL, json=_body(_MINER, manifest))
        assert bad.status_code == 400 and "license" in bad.text
        good, _ = lora_repo()
        _commit(chain, _MINER, good, 100)
        branch = await client.post(
            _SUBMIT_URL, json=_body(_MINER, good, revision="main")
        )
        assert branch.status_code == 422

    async def test_cooldown_and_resubmission(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _open_lane(client)
        fetcher = FakeFetcher()
        first = await _submit(client, chain, fetcher, _MINER, block=100)
        manifest, _ = lora_repo(rank=8)
        _commit(chain, _MINER, manifest, 200)
        again = await client.post(_SUBMIT_URL, json=_body(_MINER, manifest))
        assert again.status_code == 429 and "Retry-After" in again.headers
        terms = (
            await client.get(
                "/api/v1/model-context/upload-terms",
                params={"hotkey": _MINER.ss58_address},
            )
        ).json()
        assert terms["retry_at"] is not None

        await _skip_cooldown(session_maker)
        lora_text, _ = lora_repo()
        _commit(chain, _MINER, lora_text, 100)
        same = await client.post(_SUBMIT_URL, json=_body(_MINER, lora_text))
        assert same.status_code == 409 and first["submission_id"] in same.text

    async def test_screening_rejects_rank_and_hash_mismatch(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _open_lane(client)
        fetcher = FakeFetcher()
        high = await _submit(client, chain, fetcher, _MINER, block=100, rank=96)
        tampered = await _submit(client, chain, fetcher, _OTHER, block=101, repo="o/m")
        key = ("o/m", next(r for (repo, r) in fetcher.repos if repo == "o/m"))
        fetcher.repos[key]["adapter_config.json"] = b'{"r": 4}'
        assert sorted(await _screen_all(session_maker, fetcher)) == [
            "rejected",
            "rejected",
        ]
        rank_row = await _status(session_maker, high["submission_id"])
        assert "rank 96 is above the cap of 64" in rank_row.reject_reason
        hash_row = await _status(session_maker, tampered["submission_id"])
        assert "adapter_config.json" in hash_row.reject_reason

    async def test_unexpected_screening_error_retries_then_exhausts(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """An OSError mid-download spends an attempt and backs off like an
        outage; after MAX_SCREENING_ATTEMPTS the submission is rejected."""
        await _open_lane(client)
        accepted = await _submit(client, chain, FakeFetcher(), _MINER, block=100)

        broken = FakeFetcher()
        broken.raises = OSError(28, "No space left on device")
        outcome = None
        for attempt in range(1, MAX_SCREENING_ATTEMPTS + 1):
            outcome = await screen_next(session_maker, broken)
            assert outcome is not None
            row = await _status(session_maker, accepted["submission_id"])
            assert row.screening_attempts == attempt
            if attempt < MAX_SCREENING_ATTEMPTS:
                assert outcome[1] == "retry"
                assert row.status == "screening"
                assert row.screening_lease_until is not None
                # Backing off: nothing is claimable until the backoff ends.
                assert await screen_next(session_maker, broken) is None
                async with session_maker() as session:
                    await session.execute(
                        update(ModelContextSubmission).values(
                            screening_lease_until=None
                        )
                    )
                    await session.commit()
        assert outcome is not None and outcome[1] == "rejected"
        assert row.status == "rejected"
        assert "OSError" in row.reject_reason
        assert "No space left on device" in row.reject_reason
        assert await screen_next(session_maker, broken) is None

    async def test_unavailable_artifacts_retry_then_reject(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _open_lane(client)
        fetcher = FakeFetcher()
        accepted = await _submit(client, chain, fetcher, _MINER, block=100)
        fetcher.unavailable = True
        assert await screen_next(session_maker, fetcher) is not None
        row = await _status(session_maker, accepted["submission_id"])
        assert row.status == "screening" and row.screening_lease_until is not None
        assert await screen_next(session_maker, fetcher) is None  # backing off
        fetcher.unavailable = False
        async with session_maker() as session:
            await session.execute(
                update(ModelContextSubmission).values(screening_lease_until=None)
            )
            await session.commit()
        assert await _screen_all(session_maker, fetcher) == ["queued"]

    async def test_duplicate_weights_belong_to_the_earlier_commitment(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A copier who submits first but committed later loses the weights."""
        await _open_lane(client)
        fetcher = FakeFetcher()
        copier = await _submit(
            client, chain, fetcher, _OTHER, block=2_000, repo="copier/cpm"
        )
        assert await _screen_all(session_maker, fetcher) == ["queued"]
        original = await _submit(
            client, chain, fetcher, _MINER, block=1_500, repo="orig/cpm"
        )
        assert await _screen_all(session_maker, fetcher) == ["queued"]
        copied = await _status(session_maker, copier["submission_id"])
        assert copied.status == "rejected"
        assert copied.reject_reason.startswith("duplicate_weights")
        assert original["submission_id"] in copied.reject_reason

        # A later commitment of the same weights is rejected on arrival.
        await _skip_cooldown(session_maker)
        late = await _submit(
            client, chain, fetcher, _OTHER, block=3_000, repo="late/cpm"
        )
        assert await _screen_all(session_maker, fetcher) == ["rejected"]
        assert (await _status(session_maker, late["submission_id"])).status == (
            "rejected"
        )
        owner = await _status(session_maker, original["submission_id"])
        assert owner.status == "queued"

    async def test_new_submission_supersedes_the_active_one(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _open_lane(client)
        fetcher = FakeFetcher()
        first = await _submit(client, chain, fetcher, _MINER, block=100)
        await _screen_all(session_maker, fetcher)
        await _skip_cooldown(session_maker)
        second = await _submit(
            client, chain, fetcher, _MINER, block=200, repo="miner/v2", salt="v2"
        )
        assert (await _status(session_maker, first["submission_id"])).status == (
            "queued"
        )
        await _screen_all(session_maker, fetcher)
        assert (await _status(session_maker, first["submission_id"])).status == (
            "superseded"
        )
        assert (await _status(session_maker, second["submission_id"])).status == (
            "queued"
        )


class TestSeasonsAndScoring:
    async def test_full_lifecycle_ledger_and_board(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _open_lane(client)
        fetcher = FakeFetcher()
        a = await _submit(client, chain, fetcher, _MINER, block=1_000)
        b = await _submit(
            client, chain, fetcher, _OTHER, block=900, repo="o/cpm", salt="other"
        )
        await _screen_all(session_maker, fetcher)
        season = await _open_season(session_maker, chain)
        assert season == 1

        seasons = (await client.get("/api/v1/model-context/seasons")).json()
        assert seasons["current"] == 1
        view = seasons["seasons"][0]
        assert view["open_block"] == chain.block.number
        assert view["open_block_hash"] == _BLOCK_HASH
        assert view["seeds"] == season_seeds(1, _BLOCK_HASH)
        assert view["bench_version"] == 18

        # Reference runs (the floors) lease before any submission.
        first = (await client.get(_WORK_URL, headers=_SCORER)).json()
        second = (await client.get(_WORK_URL, headers=_SCORER)).json()
        assert {first["kind"], second["kind"]} == {"reference"}
        for work in (first, second):
            posted = await client.post(
                "/api/v1/model-context-scorer/results",
                headers=_SCORER,
                json=_result(work, quality=0.3),
            )
            assert posted.status_code == 200
        reference = (
            await client.get(
                "/api/v1/model-context-scorer/reference-work", headers=_SCORER
            )
        ).json()
        assert reference["pending"] == []
        floor = reference["base_floors"]["qwen3.5-0.8b"]

        # Equal scores: the earlier commitment block ranks first; a
        # submission that cannot clear the floor is listed but not eligible.
        works = await _score_all(
            client, {_MINER.ss58_address: 0.6, _OTHER.ss58_address: 0.6}
        )
        assert [w["kind"] for w in works] == ["submission", "submission"]
        assert works[0]["seeds"] == season_seeds(1, _BLOCK_HASH)
        assert works[0]["generate_seeds"] == [
            generate_seed(s) for s in season_seeds(1, _BLOCK_HASH)
        ]
        assert works[0]["settings"] == {
            "budget_tokens": 2048,
            "tool_k": 8,
            "floor_margin": 0.01,
        }

        ledger = await client.get(
            "/api/v1/scoring/model-context-ledger", headers=_ledger_headers()
        )
        assert ledger.status_code == 200 and ledger.headers["Cache-Control"] == (
            "no-store"
        )
        body = ledger.json()
        assert body["season"] == 1 and body["count"] == 2
        assert body["mode"] == "shadow"
        assert body["rank_shares"] == [0.5, 0.2, 0.13, 0.1, 0.07]
        assert [e["submission_id"] for e in body["entries"]] == [
            b["submission_id"],
            a["submission_id"],
        ]
        assert set(body["entries"][0]) == {
            "hotkey",
            "submission_id",
            "score",
            "eligible",
            "commitment_block",
            "season",
        }
        assert all(e["eligible"] for e in body["entries"])
        assert body["entries"][0]["score"] > floor

        board = (await client.get("/api/v1/model-context/board")).json()
        assert [e["rank"] for e in board["entries"]] == [1, 2]
        assert board["entries"][0]["model_url"].startswith(
            "https://huggingface.co/o/cpm/tree/"
        )
        assert board["base_floors"]["qwen3.5-0.8b"] == pytest.approx(floor)

        # The ledger proof is the router ledger's: no proof, no ledger.
        assert (
            await client.get("/api/v1/scoring/model-context-ledger")
        ).status_code == 401

        # Rotation: a new season re-queues both bases and both submissions.
        chain.block = BlockInfo(number=5_050_400, hash="0x" + "ef" * 32)
        forced = await client.post(
            "/api/v1/admin/model-context/seasons/rotate",
            headers=_ADMIN,
            json={
                "reason": "rotate for the test",
                "confirmation": ROTATE_SEASON_CONFIRMATION,
            },
        )
        assert forced.status_code == 200, forced.text
        assert forced.json()["season"] == 2
        assert forced.json()["seeds"] == season_seeds(2, "0x" + "ef" * 32)
        ledger2 = (
            await client.get(
                "/api/v1/scoring/model-context-ledger", headers=_ledger_headers()
            )
        ).json()
        assert ledger2["season"] == 2 and ledger2["entries"] == []
        async with session_maker() as session:
            queued = list(
                await session.scalars(
                    select(ModelContextRun.kind).where(
                        ModelContextRun.season == 2, ModelContextRun.status == "queued"
                    )
                )
            )
        assert sorted(queued) == ["reference", "reference", "submission", "submission"]

    async def test_below_floor_is_not_eligible(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _open_lane(client)
        fetcher = FakeFetcher()
        await _submit(client, chain, fetcher, _MINER, block=1_000)
        await _screen_all(session_maker, fetcher)
        await _open_season(session_maker, chain)
        await _score_all(
            client,
            {"qwen3.5-0.8b": 0.5, "qwen3.5-2b": 0.5, _MINER.ss58_address: 0.5},
        )
        (entry,) = (
            await client.get(
                "/api/v1/scoring/model-context-ledger", headers=_ledger_headers()
            )
        ).json()["entries"]
        assert entry["eligible"] is False

    async def test_results_are_checked_against_the_lease_and_contract(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _open_lane(client)
        await _open_season(session_maker, chain)
        work = (await client.get(_WORK_URL, headers=_SCORER)).json()
        url = "/api/v1/model-context-scorer/results"

        wrong_version = {**_result(work, quality=0.4), "cpm_score_version": 2}
        assert (
            await client.post(url, headers=_SCORER, json=wrong_version)
        ).status_code == 409
        wrong_seeds = _result(work, quality=0.4)
        wrong_seeds["per_seed"] = list(reversed(wrong_seeds["per_seed"]))
        assert (
            await client.post(url, headers=_SCORER, json=wrong_seeds)
        ).status_code == 409
        inconsistent = _result(work, quality=0.4)
        inconsistent["score"] = 0.99
        assert (
            await client.post(url, headers=_SCORER, json=inconsistent)
        ).status_code == 409
        out_of_range = _result(work, quality=0.4)
        out_of_range["score"] = 1.5
        assert (
            await client.post(url, headers=_SCORER, json=out_of_range)
        ).status_code == 422
        missing = _result(work, quality=0.4)
        del missing["components"]["tool_recall"]
        assert (
            await client.post(url, headers=_SCORER, json=missing)
        ).status_code == 422
        stolen = {**_result(work, quality=0.4), "lease_id": str(uuid4())}
        assert (await client.post(url, headers=_SCORER, json=stolen)).status_code == 409
        # A scorer may echo the int64 seeds it passed to cmd/generate.
        ok = await client.post(
            url, headers=_SCORER, json=_result(work, quality=0.4, int64=True)
        )
        assert ok.status_code == 200 and ok.json()["base_floor"] is not None
        replay = await client.post(
            url, headers=_SCORER, json=_result(work, quality=0.4)
        )
        assert replay.status_code == 409

        # One GPU SKU per season.
        other = (await client.get(_WORK_URL, headers=_SCORER)).json()
        sku = {**_result(other, quality=0.4), "gpu_sku": "H100"}
        assert (await client.post(url, headers=_SCORER, json=sku)).status_code == 409

    async def test_lease_expiry_and_faults(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _open_lane(client, max_attempts=2)
        fetcher = FakeFetcher()
        accepted = await _submit(client, chain, fetcher, _MINER, block=1_000)
        await _screen_all(session_maker, fetcher)
        await _open_season(session_maker, chain)
        await _score_references(client, quality=0.2)
        fault_url = "/api/v1/model-context-scorer/fault"
        work = (await client.get(_WORK_URL, headers=_SCORER)).json()
        assert work["kind"] == "submission"

        # An expired lease re-queues the run; the late result is refused.
        async with session_maker() as session:
            await session.execute(
                update(ModelContextRun)
                .where(ModelContextRun.status == "leased")
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        late = await client.post(
            "/api/v1/model-context-scorer/results",
            headers=_SCORER,
            json=_result(work, quality=0.5),
        )
        assert late.status_code == 409
        again = (await client.get(_WORK_URL, headers=_SCORER)).json()
        assert (
            again["run_id"] == work["run_id"] and again["lease_id"] != work["lease_id"]
        )

        # An infra fault past max_attempts abandons the run; nothing blames
        # the model.
        infra = await client.post(
            fault_url,
            headers=_SCORER,
            json={
                "run_id": again["run_id"],
                "lease_id": again["lease_id"],
                "error_kind": "infra",
                "detail": "CUDA OOM",
                "scorer_id": "cpm-1",
                "scorer_version": "0.1.0",
            },
        )
        assert infra.status_code == 200
        assert infra.json()["action"] == "abandoned"
        row = await _status(session_maker, accepted["submission_id"])
        assert row.status == "queued"

    async def test_model_load_fault_rejects_the_submission(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _open_lane(client)
        fetcher = FakeFetcher()
        accepted = await _submit(client, chain, fetcher, _MINER, block=1_000)
        await _screen_all(session_maker, fetcher)
        await _open_season(session_maker, chain)
        await _score_references(client, quality=0.2)
        work = (await client.get(_WORK_URL, headers=_SCORER)).json()
        resp = await client.post(
            "/api/v1/model-context-scorer/fault",
            headers=_SCORER,
            json={
                "run_id": work["run_id"],
                "lease_id": work["lease_id"],
                "error_kind": "model_load",
                "detail": "adapter shape mismatch",
                "scorer_id": "cpm-1",
                "scorer_version": "0.1.0",
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "run_id": work["run_id"],
            "action": "failed",
            "run_status": "failed",
            "submission_status": "rejected",
        }
        row = await _status(session_maker, accepted["submission_id"])
        assert row.reject_reason.startswith("scorer: model_load")

    async def test_run_on_a_removed_base_is_abandoned_not_leased(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _open_lane(client)
        fetcher = FakeFetcher()
        accepted = await _submit(client, chain, fetcher, _MINER, block=1_000)
        await _screen_all(session_maker, fetcher)
        await _open_season(session_maker, chain)
        await _score_references(client, quality=0.2)
        # The operator drops the submission's base from the allowlist while
        # its run is queued.
        await _open_lane(
            client,
            base_allowlist=[
                b
                for b in _settings()["base_allowlist"]
                if b["base_id"] != "qwen3.5-0.8b"
            ],
        )
        assert (await client.get(_WORK_URL, headers=_SCORER)).status_code == 204
        assert (await client.get(_WORK_URL, headers=_SCORER)).status_code == 204
        async with session_maker() as session:
            run = await session.scalar(
                select(ModelContextRun).where(
                    ModelContextRun.submission_id == UUID(accepted["submission_id"])
                )
            )
        assert run is not None and run.status == "abandoned"
        assert run.fault == "base qwen3.5-0.8b is no longer allowlisted"
        assert run.finished_at is not None and run.lease_id is None
        # The submission keeps its status: restoring the base re-queues it.
        row = await _status(session_maker, accepted["submission_id"])
        assert row.status == "queued"

    async def test_fault_detail_is_stored_intact_or_refused(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        assert MAX_FAULT_REQUEST_DETAIL_CHARS == 1_982
        assert MAX_FAULT_REQUEST_DETAIL_CHARS + len("nondeterministic: ") == (
            MAX_FAULT_DETAIL_CHARS
        )
        await _open_lane(client)
        await _open_season(session_maker, chain)
        work = (await client.get(_WORK_URL, headers=_SCORER)).json()
        url = "/api/v1/model-context-scorer/fault"
        body = {
            "run_id": work["run_id"],
            "lease_id": work["lease_id"],
            "error_kind": "nondeterministic",
            "scorer_id": "cpm-1",
            "scorer_version": "0.1.0",
        }
        too_long = {**body, "detail": "x" * (MAX_FAULT_REQUEST_DETAIL_CHARS + 1)}
        refused = await client.post(url, headers=_SCORER, json=too_long)
        assert refused.status_code == 422
        detail = "y" * (MAX_FAULT_REQUEST_DETAIL_CHARS - 4) + "END:"
        resp = await client.post(url, headers=_SCORER, json={**body, "detail": detail})
        assert resp.status_code == 200, resp.text
        async with session_maker() as session:
            run = await session.get(ModelContextRun, UUID(work["run_id"]))
        assert run is not None
        assert run.fault == f"nondeterministic: {detail}"
        assert len(run.fault) == MAX_FAULT_DETAIL_CHARS

    @pytest.mark.usefixtures("chain")
    async def test_scorer_auth(self, app: FastAPI, client: httpx.AsyncClient) -> None:
        assert (await client.get(_WORK_URL)).status_code == 401
        bad = {"Authorization": "Bearer " + "x" * 40}
        assert (await client.get(_WORK_URL, headers=bad)).status_code == 401
        app.state.config = replace(app.state.config, model_context_scorer_token=None)
        assert (await client.get(_WORK_URL, headers=_SCORER)).status_code == 503


_ADMIN_MC = "/api/v1/admin/model-context"


def _action(expected_status: str, confirmation: str, **extra: Any) -> dict[str, Any]:
    return {
        "expected_status": expected_status,
        "reason": "operator ruling for the test",
        "actor": "backroom:test",
        "confirmation": confirmation,
        **extra,
    }


async def _scored_pair(
    client: httpx.AsyncClient,
    chain: _Chain,
    session_maker: async_sessionmaker[AsyncSession],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Two scored submissions in season 1 above a 0.3 floor."""
    await _open_lane(client)
    fetcher = FakeFetcher()
    a = await _submit(client, chain, fetcher, _MINER, block=1_000)
    b = await _submit(
        client, chain, fetcher, _OTHER, block=900, repo="o/cpm", salt="other"
    )
    await _screen_all(session_maker, fetcher)
    await _open_season(session_maker, chain)
    await _score_all(
        client,
        {
            "qwen3.5-0.8b": 0.3,
            "qwen3.5-2b": 0.3,
            _MINER.ss58_address: 0.6,
            _OTHER.ss58_address: 0.7,
        },
    )
    return a, b


class TestOperatorInspectionAndActions:
    """The admin reads and guarded writes behind the Backroom MCP tools
    (contract section 8)."""

    async def test_list_detail_and_run_reads(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        a, b = await _scored_pair(client, chain, session_maker)
        assert (await client.get(f"{_ADMIN_MC}/submissions")).status_code in (401, 403)

        listed = (await client.get(f"{_ADMIN_MC}/submissions", headers=_ADMIN)).json()
        assert listed["counts_by_status"] == {"scored": 2}
        assert {s["submission_id"] for s in listed["submissions"]} == {
            a["submission_id"],
            b["submission_id"],
        }
        page = (
            await client.get(
                f"{_ADMIN_MC}/submissions?limit=1&offset=1", headers=_ADMIN
            )
        ).json()
        assert len(page["submissions"]) == 1
        assert page["counts_by_status"] == {"scored": 2}
        mine = (
            await client.get(
                f"{_ADMIN_MC}/submissions?hotkey={_MINER.ss58_address}",
                headers=_ADMIN,
            )
        ).json()
        assert [s["submission_id"] for s in mine["submissions"]] == [a["submission_id"]]
        none = (
            await client.get(f"{_ADMIN_MC}/submissions?status=rejected", headers=_ADMIN)
        ).json()
        assert none["submissions"] == []

        detail = (
            await client.get(
                f"{_ADMIN_MC}/submissions/{a['submission_id']}", headers=_ADMIN
            )
        ).json()
        assert detail["summary"]["status"] == "scored"
        assert detail["manifest"]["schema"] == "ditto-cpm-manifest-v1"
        assert detail["total_bytes"] > 0
        assert detail["screening_attempts"] == 1
        assert detail["actions"] == []
        (run,) = detail["runs"]
        assert run["status"] == "scored" and run["kind"] == "submission"
        assert "lease_id" not in run
        assert len(run["per_seed"]) == 3
        assert run["components"]["memory_recall"] == pytest.approx(0.6)

        run_detail = (
            await client.get(f"{_ADMIN_MC}/runs/{run['run_id']}", headers=_ADMIN)
        ).json()
        assert run_detail["run"]["run_id"] == run["run_id"]
        assert run_detail["submission"]["submission_id"] == a["submission_id"]
        assert run_detail["season"]["season"] == 1
        assert run_detail["floor"] == pytest.approx(seed_score(0.3, 0.2, 0.15))

        async with session_maker() as session:
            reference = await session.scalar(
                select(ModelContextRun.run_id).where(
                    ModelContextRun.kind == "reference"
                )
            )
        ref = (await client.get(f"{_ADMIN_MC}/runs/{reference}", headers=_ADMIN)).json()
        assert ref["submission"] is None and ref["run"]["kind"] == "reference"

        missing = uuid4()
        assert (
            await client.get(f"{_ADMIN_MC}/runs/{missing}", headers=_ADMIN)
        ).status_code == 404
        assert (
            await client.get(f"{_ADMIN_MC}/submissions/{missing}", headers=_ADMIN)
        ).status_code == 404

        scorer = (await client.get(f"{_ADMIN_MC}/scorer", headers=_ADMIN)).json()
        assert scorer["current_season"] == 1
        assert scorer["queued_runs"] == 0 and scorer["leased_runs"] == 0
        assert scorer["scorers"][0]["scorer_id"] == "cpm-1"
        assert scorer["loop_running"] is False

    async def test_rescore_is_guarded_and_audited(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        a, _ = await _scored_pair(client, chain, session_maker)
        url = f"{_ADMIN_MC}/submissions/{a['submission_id']}/rescore"

        wrong = await client.post(
            url, headers=_ADMIN, json=_action("scored", "RESCORE IT")
        )
        assert wrong.status_code == 409
        assert RESCORE_SUBMISSION_CONFIRMATION in wrong.text
        stale = await client.post(
            url,
            headers=_ADMIN,
            json=_action("queued", RESCORE_SUBMISSION_CONFIRMATION),
        )
        assert stale.status_code == 409
        assert "not queued" in stale.text

        resp = await client.post(
            url, headers=_ADMIN, json=_action("scored", RESCORE_SUBMISSION_CONFIRMATION)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["summary"]["status"] == "scored"
        assert [r["status"] for r in body["runs"]] == ["queued", "scored"]
        (action,) = body["actions"]
        assert action["action"] == "rescore"
        assert action["actor"] == "backroom:test"
        assert action["detail"] == {"status": "scored", "season": 1}

        # The standing score stays on the board until the new run lands.
        board = (await client.get("/api/v1/model-context/board")).json()
        assert a["submission_id"] in {e["submission_id"] for e in board["entries"]}

        again = await client.post(
            url, headers=_ADMIN, json=_action("scored", RESCORE_SUBMISSION_CONFIRMATION)
        )
        assert again.status_code == 409
        assert "already queued or leased" in again.text

        # The new run lands and becomes the standing score.
        await _score_all(client, {_MINER.ss58_address: 0.8})
        board = (await client.get("/api/v1/model-context/board")).json()
        assert board["entries"][0]["submission_id"] == a["submission_id"]

    async def test_rescore_needs_an_open_season(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _open_lane(client)
        fetcher = FakeFetcher()
        a = await _submit(client, chain, fetcher, _MINER, block=1_000)
        await _screen_all(session_maker, fetcher)
        resp = await client.post(
            f"{_ADMIN_MC}/submissions/{a['submission_id']}/rescore",
            headers=_ADMIN,
            json=_action("queued", RESCORE_SUBMISSION_CONFIRMATION),
        )
        assert resp.status_code == 409
        assert "no season is open" in resp.text

    async def test_reject_leaves_the_board_and_abandons_runs(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        a, b = await _scored_pair(client, chain, session_maker)
        rescored = await client.post(
            f"{_ADMIN_MC}/submissions/{b['submission_id']}/rescore",
            headers=_ADMIN,
            json=_action("scored", RESCORE_SUBMISSION_CONFIRMATION),
        )
        assert rescored.status_code == 200
        url = f"{_ADMIN_MC}/submissions/{b['submission_id']}/reject"
        wrong = await client.post(url, headers=_ADMIN, json=_action("scored", "NO"))
        assert wrong.status_code == 409
        short = await client.post(
            url,
            headers=_ADMIN,
            json={**_action("scored", REJECT_SUBMISSION_CONFIRMATION), "reason": "x"},
        )
        assert short.status_code == 422

        long_reason = "copied weights, see the evidence: " + "z" * 2_000
        resp = await client.post(
            url,
            headers=_ADMIN,
            json={
                **_action("scored", REJECT_SUBMISSION_CONFIRMATION),
                "reason": long_reason,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["summary"]["status"] == "rejected"
        assert body["summary"]["reject_reason"].startswith("operator (backroom:test): ")
        assert len(body["summary"]["reject_reason"]) <= 1_000
        assert [r["status"] for r in body["runs"]] == ["abandoned", "scored"]
        assert [x["action"] for x in body["actions"]] == ["reject", "rescore"]
        # The audit keeps the whole reason; the status reason is bounded.
        assert body["actions"][0]["reason"] == long_reason
        assert body["actions"][0]["detail"] == {"previous_status": "scored"}

        board = (await client.get("/api/v1/model-context/board")).json()
        assert [e["submission_id"] for e in board["entries"]] == [a["submission_id"]]
        ledger = (
            await client.get(
                "/api/v1/scoring/model-context-ledger", headers=_ledger_headers()
            )
        ).json()
        assert [e["submission_id"] for e in ledger["entries"]] == [a["submission_id"]]

        again = await client.post(
            url,
            headers=_ADMIN,
            json=_action("rejected", REJECT_SUBMISSION_CONFIRMATION),
        )
        assert again.status_code == 409
        assert "already rejected" in again.text
        listed = (await client.get(f"{_ADMIN_MC}/submissions", headers=_ADMIN)).json()
        assert listed["counts_by_status"] == {"scored": 1, "rejected": 1}

    async def test_reject_during_screening_wins(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _open_lane(client)
        fetcher = FakeFetcher()
        a = await _submit(client, chain, fetcher, _MINER, block=1_000)
        resp = await client.post(
            f"{_ADMIN_MC}/submissions/{a['submission_id']}/reject",
            headers=_ADMIN,
            json=_action("screening", REJECT_SUBMISSION_CONFIRMATION),
        )
        assert resp.status_code == 200, resp.text
        assert await _screen_all(session_maker, fetcher) == []
        row = await _status(session_maker, a["submission_id"])
        assert row.status == "rejected"

    async def test_forced_rotation_is_audited(
        self,
        client: httpx.AsyncClient,
        chain: _Chain,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _open_lane(client)
        await _open_season(session_maker, chain)
        chain.block = BlockInfo(number=5_050_400, hash="0x" + "ef" * 32)
        resp = await client.post(
            f"{_ADMIN_MC}/seasons/rotate",
            headers=_ADMIN,
            json={
                "reason": "a pinned base was replaced",
                "actor": "backroom:test",
                "confirmation": ROTATE_SEASON_CONFIRMATION,
            },
        )
        assert resp.status_code == 200, resp.text
        async with session_maker() as session:
            (action,) = list(await session.scalars(select(ModelContextOperatorAction)))
        assert action.action == "rotate_season"
        assert action.subject == "season:2"
        assert action.actor == "backroom:test"
        assert action.detail == {"closed_season": 1, "open_block": 5_050_400}


_TERMS_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "submission_terms_model_context.json"
)


@pytest.mark.usefixtures("chain")
async def test_submission_terms_fixture_is_the_live_response(
    client: httpx.AsyncClient,
) -> None:
    """Backroom's schema test parses this exact payload
    (``apps/backroom/src/lib/submission-terms.schemas.test.ts``), so the
    fixture must be what Platform serves. Regenerate with
    ``DITTO_REGENERATE_FIXTURES=1``."""
    body = (await client.get("/api/v1/public/submission-terms")).json()
    for terms in body["competitions"]:
        # The deposit address is deployment config, not contract.
        terms["fee"]["send_address"] = "5FixtureSendAddress"
    rendered = json.dumps(body, indent=2, sort_keys=True) + "\n"
    if os.environ.get("DITTO_REGENERATE_FIXTURES") == "1":
        _TERMS_FIXTURE.write_text(rendered)
    assert {c["competition"] for c in body["competitions"]} == {
        "memory",
        "generator",
        "model_context",
    }
    assert _TERMS_FIXTURE.read_text() == rendered


@pytest.mark.usefixtures("chain")
async def test_submission_terms_list_model_context(client: httpx.AsyncClient) -> None:
    terms = (await client.get("/api/v1/public/submission-terms/model_context")).json()
    assert terms["competition"] == "model_context"
    assert terms["accepting"] is False
    assert terms["deliverable"]["kind"] == "model_weights_repo"
    assert terms["deliverable"]["signature_payload"] == (
        "ditto-model-context-v1:{hotkey}:{hf_repo}:{revision}:"
        "{manifest_sha256}:{ts}:{nonce}"
    )
    assert terms["fee"]["required"] is False
    json.dumps(terms)
