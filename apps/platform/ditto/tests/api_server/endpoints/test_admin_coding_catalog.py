"""Admin tests for signed coding-catalog registration and retirement."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime

import bittensor
import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_catalog import (
    CodingCatalogCommitment,
    coding_catalog_commitment_signing_message,
)
from ditto.api_server.dependencies import get_session

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_CURATOR = bittensor.Keypair.create_from_uri("//Alice")


def _commitment(**overrides: object) -> CodingCatalogCommitment:
    values: dict[str, object] = {
        "schema": "dittobench-coding-catalog-commitment-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "corpus_release_id": "private-coding-corpus-v1",
        "catalog_merkle_root": "11" * 32,
        "selection_derivation_id": "coding-selection-v1",
        "selection_chain_genesis_hash": "0x" + "22" * 32,
        "grader_contract_sha256": "33" * 32,
        "inference_grant_sha256": "44" * 32,
        "task_version_count": 100,
        "curator_hotkey": _CURATOR.ss58_address,
        "committed_at_unix": int(datetime.now(UTC).timestamp()),
    }
    values.update(overrides)
    body = (
        json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode()
    values["commitment_sha256"] = hashlib.sha256(body).hexdigest()
    return CodingCatalogCommitment.model_validate(values)


def _register_payload(commitment: CodingCatalogCommitment) -> dict[str, object]:
    return {
        "commitment": commitment.model_dump(mode="json", by_alias=True),
        "signature": _CURATOR.sign(
            coding_catalog_commitment_signing_message(commitment)
        ).hex(),
        "reason": "register private coding catalog commitment",
        "actor": "operator@example.com",
        "confirmation": (
            f"REGISTER SHADOW CODING CATALOG {commitment.corpus_release_id}"
        ),
    }


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(
        app.state.config,
        admin_api_token=_ADMIN_TOKEN,
        coding_catalog_curator_hotkeys=(_CURATOR.ss58_address,),
    )

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def test_catalog_registration_is_signed_idempotent_and_retirable(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    url = "/api/v1/admin/coding-catalog/releases"
    assert (await client.get(url)).status_code == 401
    empty = await client.get(url, headers=_HEADERS)
    assert empty.status_code == 200
    assert empty.json() == {"total": 0, "releases": [], "shadow_only": True}

    commitment = _commitment()
    payload = _register_payload(commitment)
    app.state.config = replace(
        app.state.config,
        coding_catalog_curator_hotkeys=(),
    )
    disabled = await client.post(url, headers=_HEADERS, json=payload)
    assert disabled.status_code == 403
    assert "disabled" in disabled.text
    app.state.config = replace(
        app.state.config,
        coding_catalog_curator_hotkeys=(_CURATOR.ss58_address,),
    )
    bad_signature = {**payload, "signature": "00" * 64}
    assert (
        await client.post(url, headers=_HEADERS, json=bad_signature)
    ).status_code == 401
    bad_confirmation = {**payload, "confirmation": "REGISTER CODING CATALOG"}
    assert (
        await client.post(url, headers=_HEADERS, json=bad_confirmation)
    ).status_code == 422

    registered = await client.post(url, headers=_HEADERS, json=payload)
    assert registered.status_code == 200, registered.text
    record = registered.json()["releases"][0]
    assert record["commitment"]["commitment_sha256"] == commitment.commitment_sha256
    assert record["retired"] is False
    assert record["exposure_count"] == 0
    replay = await client.post(url, headers=_HEADERS, json=payload)
    assert replay.status_code == 200
    assert replay.json()["total"] == 1

    changed = _commitment(catalog_merkle_root="ff" * 32)
    conflict = await client.post(
        url,
        headers=_HEADERS,
        json=_register_payload(changed),
    )
    assert conflict.status_code == 409

    retire_url = "/api/v1/admin/coding-catalog/retire"
    retire = {
        "corpus_release_id": commitment.corpus_release_id,
        "expected_commitment_sha256": commitment.commitment_sha256,
        "reason": "retire exhausted private catalog release",
        "actor": "operator@example.com",
        "confirmation": (
            f"RETIRE SHADOW CODING CATALOG {commitment.corpus_release_id}"
        ),
    }
    stale = {**retire, "expected_commitment_sha256": "ff" * 32}
    assert (
        await client.post(retire_url, headers=_HEADERS, json=stale)
    ).status_code == 409
    retired = await client.post(retire_url, headers=_HEADERS, json=retire)
    assert retired.status_code == 200, retired.text
    retired_record = retired.json()["releases"][0]
    assert retired_record["retired"] is True
    assert retired_record["retired_actor"] == "operator@example.com"
    replay_retire = await client.post(retire_url, headers=_HEADERS, json=retire)
    assert replay_retire.status_code == 200
    assert replay_retire.json()["total"] == 1


async def test_catalog_supersession_appends_replacement_and_retirement_atomically(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    releases_url = "/api/v1/admin/coding-catalog/releases"
    previous = _commitment()
    registered = await client.post(
        releases_url,
        headers=_HEADERS,
        json=_register_payload(previous),
    )
    assert registered.status_code == 200, registered.text

    replacement = _commitment(
        corpus_release_id="private-coding-corpus-v2",
        catalog_merkle_root="55" * 32,
    )
    payload = {
        "previous_corpus_release_id": previous.corpus_release_id,
        "expected_previous_commitment_sha256": previous.commitment_sha256,
        "replacement_commitment": replacement.model_dump(mode="json", by_alias=True),
        "replacement_signature": _CURATOR.sign(
            coding_catalog_commitment_signing_message(replacement)
        ).hex(),
        "reason": "replace the reviewed private coding catalog",
        "actor": "operator@example.com",
        "confirmation": (
            "SUPERSEDE SHADOW CODING CATALOG private-coding-corpus-v1 "
            "WITH private-coding-corpus-v2"
        ),
    }
    superseded = await client.post(
        "/api/v1/admin/coding-catalog/supersede",
        headers=_HEADERS,
        json=payload,
    )
    assert superseded.status_code == 200, superseded.text
    releases = {
        item["commitment"]["corpus_release_id"]: item
        for item in superseded.json()["releases"]
    }
    assert superseded.json()["total"] == 2
    assert releases[previous.corpus_release_id]["retired"] is True
    assert releases[replacement.corpus_release_id]["retired"] is False

    replay = await client.post(
        "/api/v1/admin/coding-catalog/supersede",
        headers=_HEADERS,
        json=payload,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["total"] == 2

    stale_replacement = _commitment(
        corpus_release_id="private-coding-corpus-v3",
        catalog_merkle_root="66" * 32,
    )
    stale = {
        **payload,
        "expected_previous_commitment_sha256": "ff" * 32,
        "replacement_commitment": stale_replacement.model_dump(
            mode="json", by_alias=True
        ),
        "replacement_signature": _CURATOR.sign(
            coding_catalog_commitment_signing_message(stale_replacement)
        ).hex(),
        "confirmation": (
            "SUPERSEDE SHADOW CODING CATALOG private-coding-corpus-v1 "
            "WITH private-coding-corpus-v3"
        ),
    }
    conflict = await client.post(
        "/api/v1/admin/coding-catalog/supersede",
        headers=_HEADERS,
        json=stale,
    )
    assert conflict.status_code == 409
    final = await client.get(releases_url, headers=_HEADERS)
    assert final.json()["total"] == 2
