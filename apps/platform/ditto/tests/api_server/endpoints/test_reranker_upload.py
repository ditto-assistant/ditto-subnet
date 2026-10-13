"""``POST /upload/reranker`` and the reranker reads, against a real Postgres.

Shadow intake only: these tests also pin that an accepted reranker artifact
never becomes an ``agents`` row and is never claimable by the screener.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from dittobench_reranker.artifact import artifact_digest
from dittobench_reranker.contract import CONTRACT, CONTRACT_ID
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints import reranker_upload
from ditto.api_server.endpoints.reranker_upload import build_reranker_upload_payload
from ditto.chain.errors import ChainConnectionError
from ditto.db.models import Agent, BannedHotkey, RerankerSubmission
from ditto.db.queries.screening import claim_screening_attempts
from ditto.tests.api_server.conftest import override_get_storage_client
from ditto.tests.reranker_artifacts import (
    build_model,
    manifest,
    tarball,
    vocab_text,
)

_URL = "/api/v1/upload/reranker"
_TOKEN = "test-admin-token-at-least-32-characters"
_ADMIN = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}
_ALICE = bittensor.Keypair.create_from_uri("//Alice")
_BOB = bittensor.Keypair.create_from_uri("//Bob")


@pytest.fixture(autouse=True)
def _wire(
    app: FastAPI,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DITTO_RERANKER_UPLOADS_PER_WINDOW", raising=False)
    monkeypatch.delenv("DITTO_RERANKER_UPLOAD_WINDOW_SECONDS", raising=False)
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    _chain(app)


def _chain(
    app: FastAPI,
    *,
    registered: bool = True,
    raises: Exception | None = None,
) -> MagicMock:
    chain = MagicMock()

    async def _coldkey(hotkey: str, netuid: int) -> str | None:
        del netuid
        if raises is not None:
            raise raises
        return f"cold-{hotkey}" if registered else None

    chain.get_registered_coldkey = AsyncMock(side_effect=_coldkey)

    async def _dep() -> MagicMock:
        return chain

    app.dependency_overrides[get_chain_client] = _dep
    return chain


def _form(
    data: bytes,
    *,
    signer: bittensor.Keypair = _ALICE,
    hotkey: str | None = None,
    sha256: str | None = None,
    payload: bytes | None = None,
) -> tuple[dict[str, str], dict[str, tuple[str, bytes, str]]]:
    hotkey = hotkey or signer.ss58_address
    sha256 = sha256 or hashlib.sha256(data).hexdigest()
    timestamp = int(time.time())
    nonce = uuid4()
    signed = payload or build_reranker_upload_payload(
        hotkey=hotkey,
        sha256=sha256,
        signature_timestamp=timestamp,
        signature_nonce=nonce,
    )
    fields = {
        "hotkey": hotkey,
        "sha256": sha256,
        "signature": signer.sign(signed).hex(),
        "signature_timestamp": str(timestamp),
        "signature_nonce": str(nonce),
    }
    return fields, {"artifact": ("artifact.tar.gz", data, "application/gzip")}


async def _post(client: httpx.AsyncClient, data: bytes, **kwargs) -> httpx.Response:  # type: ignore[no-untyped-def]
    fields, files = _form(data, **kwargs)
    return await client.post(_URL, data=fields, files=files)


async def _rows(maker: async_sessionmaker[AsyncSession]) -> list[RerankerSubmission]:
    async with maker() as session:
        return list(await session.scalars(select(RerankerSubmission)))


def test_signed_payload_is_domain_separated_and_contract_bound() -> None:
    nonce = UUID("6f1c9a52-7d3e-4b8a-9c2f-1e5d7a3b9c40")
    payload = build_reranker_upload_payload(
        hotkey="5Hot", sha256="ab" * 32, signature_timestamp=7, signature_nonce=nonce
    )
    assert payload == (f"reranker-season-v1:upload:5Hot:{'ab' * 32}:7:{nonce}".encode())
    assert CONTRACT_ID == "reranker-season-v1"
    assert not payload.startswith(b"ditto-upload-v2:")


async def test_valid_upload_is_accepted_stored_and_kept_out_of_agents(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    storage = override_get_storage_client(app)
    data = tarball()

    response = await _post(client, data)

    assert response.status_code == 201, response.text
    body = response.json()
    files = {
        "model.onnx": build_model(),
        "vocab.txt": vocab_text().encode(),
        "manifest.json": json.dumps(manifest()).encode(),
    }
    expected_digest = artifact_digest(
        files["model.onnx"], files["vocab.txt"], files["manifest.json"]
    )
    assert body["status"] == "accepted"
    assert body["artifact_sha256"] == expected_digest
    assert body["upload_sha256"] == hashlib.sha256(data).hexdigest()
    assert body["contract_id"] == CONTRACT_ID
    assert body["contract_sha256"] == CONTRACT.digest()
    submission_id = UUID(body["submission_id"])

    storage.put_object.assert_awaited_once()
    call = storage.put_object.await_args.kwargs
    assert call["key"] == f"reranker/{submission_id}/artifact.tar.gz"
    assert call["body"] == data

    [row] = await _rows(session_maker)
    assert row.id == submission_id
    assert row.miner_hotkey == _ALICE.ss58_address
    assert row.miner_coldkey == f"cold-{_ALICE.ss58_address}"
    assert row.status == "accepted"
    assert row.size_bytes == len(data)
    assert (row.base_model, row.base_model_license) == ("none", "mit")
    assert row.seed is None and row.seed_block_hash is None
    assert row.score is None and row.scored_at is None

    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(Agent)) == 0
    async with session_maker() as session, session.begin():
        claimed = await claim_screening_attempts(
            session,
            screener_hotkey=_BOB.ss58_address,
            now=datetime.now(UTC),
            ttl=timedelta(minutes=45),
            limit=10,
        )
    assert claimed == []


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (
            tarball(manifest_doc=manifest(base_model_license="cc-by-nc-4.0")),
            "base_model_license",
        ),
        (tarball(manifest_doc=manifest(contract="reranker-season-v0")), "contract"),
        (tarball(model=build_model(output_name="scores")), "logits"),
        (tarball(model=b"not an onnx model"), "model.onnx"),
        (tarball(vocab="[PAD]\nhello\n"), "vocab.txt"),
        (tarball(manifest_doc=manifest(base_model="x" * 300)), "base_model"),
        (
            tarball({"model.onnx": build_model(), "vocab.txt": b"[PAD]\n"}),
            "missing",
        ),
        (
            tarball({f"junk{i}": b"" for i in range(20)}),
            "too many entries",
        ),
        (b"\x1f\x8bnot really gzip at all", "not a readable tar"),
        (b"plain bytes, not a tar", "not a readable tar"),
    ],
    ids=[
        "non-commercial-licence",
        "wrong-contract",
        "no-logits-output",
        "unparseable-model",
        "special-tokens-misplaced",
        "base-model-too-long",
        "missing-manifest",
        "tar-of-empty-entries",
        "corrupt-gzip",
        "not-a-tar",
    ],
)
async def test_invalid_artifact_is_rejected_with_its_reason(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    data: bytes,
    reason: str,
) -> None:
    storage = override_get_storage_client(app)

    response = await _post(client, data)

    assert response.status_code == 422, response.text
    message = response.json()["message"]
    assert message.startswith("artifact rejected: ")
    assert reason in message
    assert len(message) < 600
    storage.put_object.assert_not_awaited()
    assert await _rows(session_maker) == []


async def test_gzip_bomb_is_refused_at_the_decompressed_cap(
    app: FastAPI,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = override_get_storage_client(app)
    monkeypatch.setattr(reranker_upload, "MAX_RERANKER_UPLOAD_BYTES", 256 * 1024)
    zeros = b"\0" * (4 * 1024 * 1024)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("model.onnx")
        info.size = len(zeros)
        tar.addfile(info, io.BytesIO(zeros))
    data = buf.getvalue()
    assert len(data) < 256 * 1024

    response = await _post(client, data)

    assert response.status_code == 422, response.text
    assert "size limit" in response.json()["message"]
    storage.put_object.assert_not_awaited()


async def test_exact_duplicate_artifact_is_rejected_first_submitter_wins(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    storage = override_get_storage_client(app)
    data = tarball()
    first = await _post(client, data, signer=_ALICE)
    assert first.status_code == 201, first.text

    second = await _post(client, data, signer=_BOB)

    assert second.status_code == 409, second.text
    assert first.json()["submission_id"] in second.json()["message"]
    assert storage.put_object.await_count == 1
    [row] = await _rows(session_maker)
    assert row.miner_hotkey == _ALICE.ss58_address


async def test_signature_by_another_key_is_rejected(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    storage = override_get_storage_client(app)

    response = await _post(client, tarball(), signer=_BOB, hotkey=_ALICE.ss58_address)

    assert response.status_code == 401
    storage.put_object.assert_not_awaited()


async def test_memory_track_signature_cannot_be_replayed_here(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    override_get_storage_client(app)
    data = tarball()
    sha256 = hashlib.sha256(data).hexdigest()
    timestamp = int(time.time())
    nonce = uuid4()
    memory_payload = (
        f"ditto-upload-v2:{_ALICE.ss58_address}:{sha256}:{timestamp}:{nonce}"
    ).encode("ascii")
    fields = {
        "hotkey": _ALICE.ss58_address,
        "sha256": sha256,
        "signature": _ALICE.sign(memory_payload).hex(),
        "signature_timestamp": str(timestamp),
        "signature_nonce": str(nonce),
    }

    response = await client.post(
        _URL,
        data=fields,
        files={"artifact": ("a.tar.gz", data, "application/gzip")},
    )

    assert response.status_code == 401


async def test_stale_signature_is_rejected(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    override_get_storage_client(app)
    data = tarball()
    sha256 = hashlib.sha256(data).hexdigest()
    stale = int(time.time()) - 3600
    nonce = uuid4()
    payload = build_reranker_upload_payload(
        hotkey=_ALICE.ss58_address,
        sha256=sha256,
        signature_timestamp=stale,
        signature_nonce=nonce,
    )
    fields = {
        "hotkey": _ALICE.ss58_address,
        "sha256": sha256,
        "signature": _ALICE.sign(payload).hex(),
        "signature_timestamp": str(stale),
        "signature_nonce": str(nonce),
    }
    response = await client.post(
        _URL, data=fields, files={"artifact": ("a", data, "application/gzip")}
    )
    assert response.status_code == 401


async def test_unregistered_hotkey_is_rejected(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    storage = override_get_storage_client(app)
    _chain(app, registered=False)

    response = await _post(client, tarball())

    assert response.status_code == 403
    assert "not registered" in response.json()["message"]
    storage.put_object.assert_not_awaited()
    assert await _rows(session_maker) == []


async def test_chain_outage_is_503(app: FastAPI, client: httpx.AsyncClient) -> None:
    override_get_storage_client(app)
    _chain(app, raises=ChainConnectionError("down"))

    response = await _post(client, tarball())

    assert response.status_code == 503
    assert "down" not in response.json()["message"]


async def test_banned_hotkey_is_rejected(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    storage = override_get_storage_client(app)
    async with session_maker() as session, session.begin():
        session.add(BannedHotkey(hotkey=_ALICE.ss58_address, reason="test"))

    response = await _post(client, tarball())

    assert response.status_code == 403
    assert "banned" in response.json()["message"]
    storage.put_object.assert_not_awaited()


async def test_over_size_upload_is_rejected_while_streaming(
    app: FastAPI,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = override_get_storage_client(app)
    data = tarball()
    monkeypatch.setattr(reranker_upload, "MAX_RERANKER_UPLOAD_BYTES", len(data) - 1)

    response = await _post(client, data)

    assert response.status_code == 413
    storage.put_object.assert_not_awaited()


def test_default_upload_cap_covers_the_contract_limits() -> None:
    limits = CONTRACT.artifact
    assert (
        limits.max_model_bytes + limits.max_vocab_bytes + limits.max_manifest_bytes
    ) < reranker_upload.MAX_RERANKER_UPLOAD_BYTES
    assert reranker_upload.MAX_RERANKER_UPLOAD_BYTES <= 104 * 1024 * 1024


async def test_declared_sha256_must_match_the_bytes(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    override_get_storage_client(app)

    response = await _post(client, tarball(), sha256="0" * 64)

    assert response.status_code == 400
    assert "sha256" in response.json()["message"]


async def test_one_upload_per_owner_window_by_default(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    storage = override_get_storage_client(app)
    first = await _post(client, tarball())
    assert first.status_code == 201, first.text

    second = await _post(client, tarball(model=build_model(needle_weight=2.0)))

    assert second.status_code == 429, second.text
    retry_after = int(second.headers["Retry-After"])
    assert 0 < retry_after <= 3600 + 1
    assert storage.put_object.await_count == 1
    assert len(await _rows(session_maker)) == 1


async def test_refused_uploads_do_not_consume_the_owner_window(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    storage = override_get_storage_client(app)
    invalid = await _post(
        client, tarball(manifest_doc=manifest(base_model_license="cc-by-nc-4.0"))
    )
    mismatched = await _post(client, tarball(), sha256="0" * 64)
    assert invalid.status_code == 422, invalid.text
    assert mismatched.status_code == 400, mismatched.text

    # Only an accepted submission takes the slot.
    accepted = await _post(client, tarball())
    assert accepted.status_code == 201, accepted.text
    again = await _post(client, tarball(model=build_model(needle_weight=3.0)))
    assert again.status_code == 429, again.text
    assert storage.put_object.await_count == 1
    assert len(await _rows(session_maker)) == 1


async def test_upload_window_is_configurable(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override_get_storage_client(app)
    monkeypatch.setenv("DITTO_RERANKER_UPLOADS_PER_WINDOW", "2")

    for weight in (1.0, 2.0):
        response = await _post(client, tarball(model=build_model(weight)))
        assert response.status_code == 201, response.text
    third = await _post(client, tarball(model=build_model(3.0)))

    assert third.status_code == 429
    assert len(await _rows(session_maker)) == 2


async def test_public_reads_report_status_without_tarball_urls(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    override_get_storage_client(app)
    accepted = (await _post(client, tarball())).json()

    listing = await client.get(
        "/api/v1/public/reranker/submissions",
        params={"hotkey": _ALICE.ss58_address},
    )
    one = await client.get(
        f"/api/v1/public/reranker/submissions/{accepted['submission_id']}"
    )
    other = await client.get(
        "/api/v1/public/reranker/submissions",
        params={"hotkey": _BOB.ss58_address},
    )
    missing = await client.get(f"/api/v1/public/reranker/submissions/{uuid4()}")

    assert listing.status_code == 200
    page = listing.json()
    assert page["total"] == 1 and page["limit"] == 50 and page["offset"] == 0
    [item] = page["submissions"]
    assert item == one.json()
    assert item["submission_id"] == accepted["submission_id"]
    assert item["status"] == "accepted"
    assert item["score"] is None and item["seed"] is None
    assert "upload_sha256" not in item and "miner_coldkey" not in item
    assert not any("url" in key for key in item)
    assert "max-age" in listing.headers["cache-control"]
    assert other.json()["total"] == 0
    assert missing.status_code == 404


async def test_admin_read_requires_the_admin_token_and_counts_statuses(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    override_get_storage_client(app)
    await _post(client, tarball())
    async with session_maker() as session, session.begin():
        session.add(
            RerankerSubmission(
                id=uuid4(),
                miner_hotkey=_BOB.ss58_address,
                miner_coldkey="cold-bob",
                upload_sha256="1" * 64,
                artifact_sha256="2" * 64,
                contract_sha256=CONTRACT.digest(),
                size_bytes=10,
                base_model="none",
                base_model_license="mit",
                status="scored",
                seed=2**63 - 1,
                score={"ndcg_at_10": 0.8},
            )
        )

    denied = await client.get("/api/v1/admin/reranker-submissions")
    response = await client.get("/api/v1/admin/reranker-submissions", headers=_ADMIN)

    assert denied.status_code == 401
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contract_id"] == CONTRACT_ID
    assert body["status_counts"] == {
        "accepted": 1,
        "invalid": 0,
        "scored": 1,
        "failed": 0,
    }
    assert body["total"] == 2
    assert body["upload_window_seconds"] == 3600
    assert body["uploads_per_window"] == 1
    scored = next(s for s in body["submissions"] if s["status"] == "scored")
    assert scored["seed"] == str(2**63 - 1)
    assert scored["score"] == {"ndcg_at_10": 0.8}
    assert scored["miner_coldkey"] == "cold-bob"
    filtered = await client.get(
        "/api/v1/admin/reranker-submissions",
        params={"status": "accepted"},
        headers=_ADMIN,
    )
    assert filtered.json()["total"] == 1
