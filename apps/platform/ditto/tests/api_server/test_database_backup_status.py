"""Recovery metadata distinguishes missing, stale and failed provider reads."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from ditto.api_models.database_backup import (
    DatabaseBackupManifest,
    DatabaseBackupStatus,
)
from ditto.api_server import database_backup as service
from ditto.api_server.endpoints import admin_database_backup as endpoint
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.hippius import HippiusClient, ObjectSummary
from ditto.api_server.storage.errors import ObjectDownloadFailedError


def manifest(now):
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return DatabaseBackupManifest(
        format_version=1,
        database="ditto_platform_prod",
        server_version="PostgreSQL 17",
        server_version_num=170004,
        pg_dump_version="pg_dump (PostgreSQL) 17",
        database_bytes=1234,
        alembic_version="abcdef",
        row_counts={"agents": 100, "screening_attempts": 200, "scores": 300},
        started_at=now,
        completed_at=now + timedelta(minutes=1),
        objects=[
            {
                "name": f"ditto_platform_prod-{stamp}.dump.age",
                "size": 100,
                "sha256": "a" * 64,
            },
            {"name": f"globals-{stamp}.sql.age", "size": 50, "sha256": "b" * 64},
        ],
    )


def status(now):
    return DatabaseBackupStatus(
        observed_at=now, backup_status="unavailable", snapshot_status="unavailable"
    )


@pytest.mark.parametrize("hours,expected", [(2, "fresh"), (37, "stale")])
async def test_metadata_freshness_and_separate_reader(monkeypatch, hours, expected):
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    source = now - timedelta(hours=hours)
    data = manifest(source)
    prefix = source.strftime("daily/%Y/%m/%d/")
    marker = prefix + source.strftime("manifest-%Y%m%dT%H%M%SZ.json")
    rows = [
        ObjectSummary(key=marker, size=900, last_modified=source.isoformat(), etag=""),
        *[
            ObjectSummary(
                key=prefix + obj.name,
                size=obj.size,
                last_modified=source.isoformat(),
                etag="",
            )
            for obj in data.objects
        ],
    ]
    monkeypatch.setenv("DATABASE_BACKUP_READER_ACCESS_KEY_ID", "reader-only-fixture")
    monkeypatch.setenv(
        "DATABASE_BACKUP_READER_SECRET_ACCESS_KEY", "reader-secret-fixture"
    )
    monkeypatch.setattr(
        HippiusClient, "list_objects", AsyncMock(side_effect=[(rows, None), ([], None)])
    )
    monkeypatch.setattr(
        HippiusClient,
        "get_object",
        AsyncMock(return_value=data.model_dump_json().encode()),
    )
    result = status(now)
    await service.read_backups(result)
    assert result.backup_status == expected
    assert result.manifest == data
    assert result.hours_since_last_success is not None
    assert "reader-secret-fixture" not in result.model_dump_json()


async def test_partial_commit_never_reports_fresh(monkeypatch):
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    source = now - timedelta(hours=2)
    data = manifest(source)
    marker = source.strftime("daily/%Y/%m/%d/manifest-%Y%m%dT%H%M%SZ.json")
    monkeypatch.setenv("DATABASE_BACKUP_READER_ACCESS_KEY_ID", "fixture")
    monkeypatch.setenv("DATABASE_BACKUP_READER_SECRET_ACCESS_KEY", "fixture")
    rows = [ObjectSummary(key=marker, size=900, last_modified="", etag="")]
    monkeypatch.setattr(
        HippiusClient, "list_objects", AsyncMock(side_effect=[(rows, None), ([], None)])
    )
    monkeypatch.setattr(
        HippiusClient,
        "get_object",
        AsyncMock(return_value=data.model_dump_json().encode()),
    )
    with pytest.raises(ValueError, match="missing encrypted"):
        await service.read_backups(status(now))


async def test_provider_failure_is_explicit_and_never_leaks(monkeypatch):
    monkeypatch.setattr(
        service,
        "read_backups",
        AsyncMock(side_effect=RuntimeError("secret-token-in-provider-url")),
    )
    monkeypatch.setattr(
        service, "newest_snapshot", AsyncMock(side_effect=RuntimeError("Bearer secret"))
    )
    result = await service.backup_status()
    assert result.backup_status == "unavailable"
    assert result.snapshot_status == "unavailable"
    assert result.hours_since_last_success is None
    assert "secret" not in result.model_dump_json()


async def test_disabled_reader_does_not_fall_back_to_avatar_keys(monkeypatch):
    monkeypatch.delenv("DATABASE_BACKUP_READER_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("DATABASE_BACKUP_READER_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("HIPPIUS_ACCESS_KEY_ID", "avatar-reader")
    monkeypatch.setenv("HIPPIUS_SECRET_ACCESS_KEY", "avatar-secret")
    result = status(datetime.now(UTC))
    await service.read_backups(result)
    assert result.backup_status == "disabled"


async def test_incomplete_inventory_is_not_a_missing_backup():
    client = AsyncMock()
    client.list_objects.return_value = ([], "repeated-token")
    with pytest.raises(ValueError, match="pagination incomplete"):
        await service.inventory(client, "daily/")


def test_manifest_identity_and_time_are_validated():
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    source = now - timedelta(hours=2)
    data = manifest(source)
    key = source.strftime("daily/%Y/%m/%d/manifest-%Y%m%dT%H%M%SZ.json")
    assert service.manifest_age(data, now, key) == 2
    with pytest.raises(ValueError):
        service.manifest_age(data, now, key.replace("/10/04/", "/10/03/"))
    with pytest.raises(ValueError):
        service.manifest_age(data, source - timedelta(hours=1), key)


async def test_metadata_read_is_bounded_after_listing():
    client = object.__new__(HippiusClient)
    client._presign = AsyncMock(return_value="https://test.invalid/synthetic")
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"x" * 100),
        )
    )
    try:
        with pytest.raises(ObjectDownloadFailedError, match="exceeded bound"):
            await client.get_object(key="manifest.json", max_bytes=10)
    finally:
        await client._http.aclose()


async def test_endpoint_requires_admin_and_is_no_store(monkeypatch):
    app = FastAPI()
    app.state.config = SimpleNamespace(admin_api_token="test-admin")
    app.include_router(endpoint.router, prefix="/api/v1")
    result = status(datetime.now(UTC))
    monkeypatch.setattr(endpoint, "backup_status", AsyncMock(return_value=result))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.get("/api/v1/admin/database-backup-status")
        assert denied.status_code == 401
        app.dependency_overrides[require_admin] = lambda: None
        allowed = await client.get("/api/v1/admin/database-backup-status")
        assert allowed.status_code == 200
        assert allowed.headers["cache-control"] == "no-store"
        assert allowed.json()["backup_status"] == "unavailable"
