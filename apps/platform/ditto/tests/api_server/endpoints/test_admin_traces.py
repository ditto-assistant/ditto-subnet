"""Admin access to the inference trace archive: listing, presigned downloads,
and the bounded server-side peek. The Hippius wire path is stubbed at
``app.state.traces_hippius``; the zstd framing, key discipline, actor audit,
and bounding behavior under test are real."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass

import httpx
import pytest
import zstandard
from fastapi import FastAPI

from ditto.api_server.hippius import ObjectSummary, _parse_list_objects

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {
    "Authorization": f"Bearer {_ADMIN_TOKEN}",
    "X-Admin-Actor": "operator@example.com",
}


@dataclass
class _StubTraceStore:
    bucket: str = "ditto-subnet-traces"
    objects: dict[str, bytes] | None = None
    listed: list[tuple[str, int, str | None]] | None = None

    def __post_init__(self) -> None:
        self.objects = self.objects or {}
        self.listed = []

    async def list_objects(self, *, prefix="", max_keys=200, continuation_token=None):
        self.listed.append((prefix, max_keys, continuation_token))
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        page = [
            ObjectSummary(
                key=k,
                size=len(self.objects[k]),
                last_modified="2026-08-24T00:00:00.000Z",
                etag="e",
            )
            for k in keys[:max_keys]
        ]
        next_token = "next-token" if len(keys) > max_keys else None
        return page, next_token

    async def presigned_get_url(self, *, key, expires_in=300):
        return f"https://eu-central-1.hippius.com/{self.bucket}/{key}?X-Amz-Signature=stub&X-Amz-Expires={expires_in}"

    async def get_object(self, *, key):
        from ditto.api_server.storage.errors import ObjectNotFoundError

        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self.objects[key]


def _record(i: int, case: str | None = None) -> bytes:
    return json.dumps(
        {
            "schema": "ditto.inference.trace.v1",
            "event": "inference.settled",
            "recorded_at": f"2026-08-24T00:00:{i:02d}Z",
            "request": {
                "lane": "inference",
                "kind": "chat",
                "grant_id": "g",
                "nonce": f"n{i}",
                "run_id": "run-1",
                "case_id": case,
                "body": {"messages": [{"role": "user", "content": "hi " * 50}]},
                "body_bytes": 42,
            },
            "grant": {
                "agent_id": "a",
                "validator_hotkey": "hk",
                "bench_version": 12,
                "status": "active",
                "generation": 1,
                "request_count": i,
            },
            "usage": {
                "prompt_tokens": 10 + i,
                "completion_tokens": 5,
                "cost_microusd": 1,
                "usage_available": True,
            },
            "upstream": {
                "provider": "deepinfra",
                "latency_ms": 800,
                "attempts": 1,
                "fallback_phase": 0,
                "timed_out": False,
            },
            "outcome": {"status": "completed"},
        }
    ).encode()


def _object_bytes(n: int) -> bytes:
    lines = b"".join(
        _record(i, case="web_search-0001" if i % 2 else None) + b"\n" for i in range(n)
    )
    return zstandard.ZstdCompressor().compress(lines)


def _install(app: FastAPI, store: _StubTraceStore | None) -> None:
    from dataclasses import replace

    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.traces_hippius = store


async def test_listing_builds_partition_prefixes_and_pages(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    chat = "traces/v1/lane=inference/kind=chat/dt=2026-08-24"
    embedding = "traces/v1/lane=inference/kind=embedding/dt=2026-08-24"
    store = _StubTraceStore(
        objects={
            f"{chat}/hour=00/a.jsonl.zst": b"x",
            f"{chat}/hour=01/b.jsonl.zst": b"y",
            f"{embedding}/hour=00/c.jsonl.zst": b"z",
        }
    )
    _install(app, store)
    response = await client.get(
        "/api/v1/admin/traces",
        params={"lane": "inference", "kind": "chat", "dt": "2026-08-24"},
        headers=_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["prefix"] == "traces/v1/lane=inference/kind=chat/dt=2026-08-24/"
    assert [o["key"].rsplit("/", 1)[1] for o in body["objects"]] == [
        "a.jsonl.zst",
        "b.jsonl.zst",
    ]
    assert body["continuation_token"] is None
    assert body["bucket"] == "ditto-subnet-traces"

    # A partition level below a missing one is refused, not silently ignored.
    holey = await client.get(
        "/api/v1/admin/traces",
        params={"lane": "inference", "dt": "2026-08-24"},
        headers=_HEADERS,
    )
    assert holey.status_code == 422
    # A raw prefix outside the archive roots is refused.
    outside = await client.get(
        "/api/v1/admin/traces", params={"prefix": "agents/"}, headers=_HEADERS
    )
    assert outside.status_code == 422
    # The ledger scope is a first-class root.
    ledger = await client.get(
        "/api/v1/admin/traces", params={"scope": "ledger"}, headers=_HEADERS
    )
    assert ledger.status_code == 200
    assert ledger.json()["prefix"] == "ledger/v1/"


async def test_download_url_requires_actor_and_stays_inside_the_archive(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    store = _StubTraceStore()
    _install(app, store)
    key = "traces/v1/lane=inference/kind=chat/dt=2026-08-24/hour=00/a.jsonl.zst"
    no_actor = await client.post(
        "/api/v1/admin/traces/download-url",
        json={"key": key},
        headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
    )
    assert no_actor.status_code == 422
    escape = await client.post(
        "/api/v1/admin/traces/download-url",
        json={"key": "../agents/secret.tar.gz"},
        headers=_HEADERS,
    )
    assert escape.status_code == 422
    ok = await client.post(
        "/api/v1/admin/traces/download-url",
        json={"key": key, "expires_in": 600},
        headers=_HEADERS,
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["key"] == key and body["expires_in"] == 600
    assert body["url"].startswith("https://") and "X-Amz-Signature" in body["url"]


async def test_peek_returns_bounded_summaries_and_optional_bodies(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    key = "traces/v1/lane=inference/kind=chat/dt=2026-08-24/hour=00/a.jsonl.zst"
    store = _StubTraceStore(objects={key: _object_bytes(20)})
    _install(app, store)
    response = await client.post(
        "/api/v1/admin/traces/peek",
        json={"key": key, "max_records": 3, "offset_records": 4},
        headers=_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [r["index"] for r in body["records"]] == [4, 5, 6]
    first = body["records"][0]
    assert (
        first["kind"] == "chat"
        and first["run_id"] == "run-1"
        and first["prompt_tokens"] == 14
    )
    assert first["provider"] == "deepinfra" and first["status"] == "completed"
    assert first["record"] is None  # summaries never carry bodies by default
    assert body["scan_complete"] is True

    with_bodies = await client.post(
        "/api/v1/admin/traces/peek",
        json={"key": key, "max_records": 1, "include_bodies": True},
        headers=_HEADERS,
    )
    record = with_bodies.json()["records"][0]["record"]
    assert (
        record is not None
        and record["request"]["body"]["messages"][0]["role"] == "user"
    )

    missing = await client.post(
        "/api/v1/admin/traces/peek",
        json={"key": "traces/v1/nope.jsonl.zst"},
        headers=_HEADERS,
    )
    assert missing.status_code == 404


async def test_unconfigured_storage_is_a_503_not_a_crash(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    _install(app, None)
    response = await client.get("/api/v1/admin/traces", headers=_HEADERS)
    assert response.status_code == 503


def test_list_objects_xml_parse_handles_namespaces_and_pagination() -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        "<Name>ditto-subnet-traces</Name><IsTruncated>true</IsTruncated>"
        "<NextContinuationToken>tok-2</NextContinuationToken>"
        "<Contents><Key>traces/v1/a.jsonl.zst</Key><Size>123</Size>"
        '<LastModified>2026-08-24T00:00:00.000Z</LastModified><ETag>"abc"</ETag></Contents>'
        "</ListBucketResult>"
    )
    objects, token = _parse_list_objects(xml.encode())
    assert token == "tok-2"
    assert objects == [
        ObjectSummary(
            key="traces/v1/a.jsonl.zst",
            size=123,
            last_modified="2026-08-24T00:00:00.000Z",
            etag="abc",
        )
    ]
    _ = io  # silence unused-import lint in case of refactors
