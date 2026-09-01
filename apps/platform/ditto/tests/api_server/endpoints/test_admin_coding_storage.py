from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI

from ditto.api_models.coding_storage_readiness import (
    AdminCodingStorageReadinessResponse,
    CodingStorageAuthorityReadiness,
)

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


class _Probe:
    async def snapshot(self) -> AdminCodingStorageReadinessResponse:
        ready = CodingStorageAuthorityReadiness(
            status="ready",
            sha256="ab" * 32,
            size_bytes=64,
            exact_object_verified=True,
        )
        return AdminCodingStorageReadinessResponse(
            schema="dittobench-coding-storage-readiness-v1",
            environment="dev",
            source_sha="cd" * 20,
            checked_at=datetime(2026, 9, 1, tzinfo=UTC),
            ready=True,
            private_input=ready,
            sealed_evidence=ready,
            authorities_distinct=True,
            read_only=True,
            weight_eligible=False,
        )


async def test_readiness_is_admin_only_no_store_and_default_off(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    url = "/api/v1/admin/coding-storage/readiness"
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    assert (await client.get(url)).status_code == 401
    disabled = await client.get(url, headers=_HEADERS)
    assert disabled.status_code == 503
    assert disabled.headers["cache-control"] == "no-store"

    app.state.coding_storage_readiness_probe = _Probe()
    response = await client.get(url, headers=_HEADERS)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["ready"] is True
    assert payload["read_only"] is True
    assert payload["weight_eligible"] is False
    assert "bucket" not in response.text
    assert "key" not in response.text
