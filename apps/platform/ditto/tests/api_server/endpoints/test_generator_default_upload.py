"""Default generator registration through a presigned upload.

A starter-kit archive with its vendored datagen is too large to inline through
an MCP tool argument, so an operator PUTs it to a presigned URL under their own
prefix and registers it by key. The Hippius wire path is stubbed at
``app.state.traces_hippius``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.generator import MAX_SOURCE_ARCHIVE_BYTES
from ditto.api_models.generator_rounds import (
    DEFAULT_GENERATOR_UPLOAD_PREFIX,
    REGISTER_DEFAULT_GENERATOR_CONFIRMATION,
)
from ditto.api_server.storage.errors import (
    ObjectDownloadFailedError,
    ObjectNotFoundError,
)
from ditto.db.models import GeneratorOperatorAction, GeneratorSubmission
from ditto.tests.api_server.endpoints.test_generator_competition import (
    _ADMIN,
    _archive,
)
from ditto.tests.api_server.endpoints.test_generator_rounds import (
    _VALIDATOR,
    _install,
)

_UPLOAD_URL = "/api/v1/admin/generator-competition/default-generator/upload-url"
_REGISTER_URL = "/api/v1/admin/generator-competition/default-generator"
_ACTOR = "Operator@Example.com"
_ACTOR_HEADERS = {**_ADMIN, "X-Admin-Actor": _ACTOR}


@dataclass
class _StubStore:
    bucket: str = "ditto-subnet-traces"
    objects: dict[str, bytes] = field(default_factory=dict)
    presigned: list[tuple[str, str, int]] = field(default_factory=list)

    async def presigned_put_url(
        self, *, key: str, content_type: str, expires_in: int = 300
    ) -> str:
        self.presigned.append((key, content_type, expires_in))
        return f"https://s3.hippius.com/{self.bucket}/{key}?X-Amz-Signature=stub"

    async def get_object(self, *, key: str, max_bytes: int | None = None) -> bytes:
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        body = self.objects[key]
        if max_bytes is not None and len(body) > max_bytes:
            raise ObjectDownloadFailedError("exceeded bound")
        return body


def _register_body(raw: bytes, key: str, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "miner_hotkey": _VALIDATOR.ss58_address,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "upload_key": key,
        "reason": "register the starter kit by upload",
        "actor": _ACTOR,
        "confirmation": REGISTER_DEFAULT_GENERATOR_CONFIRMATION,
    }
    body.update(overrides)
    return body


async def _issue(client: httpx.AsyncClient, actor: str = _ACTOR) -> str:
    resp = await client.post(_UPLOAD_URL, headers={**_ADMIN, "X-Admin-Actor": actor})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["key"])


async def test_registers_an_uploaded_archive(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    store = _StubStore()
    app.state.traces_hippius = store
    resp = await client.post(_UPLOAD_URL, headers={**_ADMIN, "X-Admin-Actor": _ACTOR})
    assert resp.status_code == 200, resp.text
    issued = resp.json()
    assert issued["method"] == "PUT"
    assert issued["content_type"] == "application/gzip"
    assert issued["max_bytes"] == MAX_SOURCE_ARCHIVE_BYTES
    assert issued["key"].startswith(
        f"{DEFAULT_GENERATOR_UPLOAD_PREFIX}operator-example.com-"
    )
    assert store.presigned[0][1] == "application/gzip"

    raw = _archive("uploaded-default")
    store.objects[issued["key"]] = raw
    reg = await client.post(
        _REGISTER_URL, headers=_ACTOR_HEADERS, json=_register_body(raw, issued["key"])
    )
    assert reg.status_code == 200, reg.text
    async with session_maker() as session:
        row = await session.get(GeneratorSubmission, reg.json()["submission_id"])
        assert row is not None and row.public_default and row.source_bytes == raw
        action = await session.scalar(
            select(GeneratorOperatorAction).where(
                GeneratorOperatorAction.action == "register_default_generator"
            )
        )
        assert action is not None and action.detail is not None
        assert action.detail["upload_key"] == issued["key"]


async def test_refuses_another_operators_key(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    store = _StubStore()
    app.state.traces_hippius = store
    raw = _archive("not-mine")
    # A distinct actor whose slug folds to the same text, and a body that
    # claims to be the issuer, are both refused: scope follows the header.
    for issuer in ("operator@example.com", "someone-else@example.com"):
        key = await _issue(client, actor=issuer)
        store.objects[key] = raw
        resp = await client.post(
            _REGISTER_URL,
            headers=_ACTOR_HEADERS,
            json=_register_body(raw, key, actor=issuer),
        )
        assert resp.status_code == 403, resp.text
    async with session_maker() as session:
        assert (await session.scalar(select(GeneratorSubmission))) is None


@pytest.mark.parametrize(
    ("mutate", "status"),
    [
        ("missing", 404),
        ("sha", 422),
        ("oversize", 422),
    ],
)
async def test_refuses_a_bad_upload(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    mutate: str,
    status: int,
) -> None:
    _install(app, session_maker)
    store = _StubStore()
    app.state.traces_hippius = store
    key = await _issue(client)
    raw = _archive(mutate)
    body = _register_body(raw, key)
    if mutate == "sha":
        store.objects[key] = raw
        body["source_sha256"] = "0" * 64
    elif mutate == "oversize":
        store.objects[key] = b"\0" * (MAX_SOURCE_ARCHIVE_BYTES + 1)
    resp = await client.post(_REGISTER_URL, headers=_ACTOR_HEADERS, json=body)
    assert resp.status_code == status, resp.text
    async with session_maker() as session:
        assert (await session.scalar(select(GeneratorSubmission))) is None


async def test_requires_exactly_one_source(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    raw = _archive("both")
    both = _register_body(raw, "generator-default/v1/x/y.tar.gz", source_base64="AAAA")
    neither = _register_body(raw, "unused")
    del neither["upload_key"]
    for body in (both, neither):
        resp = await client.post(_REGISTER_URL, headers=_ADMIN, json=body)
        assert resp.status_code == 422, resp.text


async def test_upload_needs_storage_and_an_actor(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    app.state.traces_hippius = None
    no_store = await client.post(
        _UPLOAD_URL, headers={**_ADMIN, "X-Admin-Actor": _ACTOR}
    )
    assert no_store.status_code == 503
    app.state.traces_hippius = _StubStore()
    no_actor = await client.post(_UPLOAD_URL, headers=_ADMIN)
    assert no_actor.status_code == 422
