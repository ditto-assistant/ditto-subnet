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
from ditto.api_server.storage.errors import (
    ObjectDownloadFailedError,
    ObjectNotFoundError,
)


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


@pytest.mark.parametrize(
    "hours,expected,completed_hours",
    [(2, "fresh", None), (37, "stale", None), (37, "stale", 1)],
)
async def test_metadata_freshness_and_separate_reader(
    monkeypatch, hours, expected, completed_hours
):
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    source = now - timedelta(hours=hours)
    data = manifest(source)
    if completed_hours is not None:
        data.completed_at = now - timedelta(hours=completed_hours)
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


async def test_snapshot_reader_paginates_filtered_results_without_server_sort(
    monkeypatch,
):
    seen = []
    real_client = httpx.AsyncClient

    def respond(request):
        if request.url.host == "metadata.google.internal":
            return httpx.Response(200, json={"access_token": "synthetic-token"})
        seen.append(request)
        if "orderBy" in request.url.params:
            return httpx.Response(400, json={"error": "filter plus sort unsupported"})
        assert request.url.params["filter"] == f'sourceDisk = "{service._DISK}"'
        assert request.url.params["maxResults"] == "100"
        assert "nextPageToken" in request.url.params["fields"]
        if "pageToken" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "name": "older",
                            "creationTimestamp": "2026-10-06T23:23:12-07:00",
                            "status": "READY",
                            "diskSizeGb": "100",
                        }
                    ],
                    "nextPageToken": "opaque/cursor+value",
                },
            )
        assert request.url.params["pageToken"] == "opaque/cursor+value"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "name": "newest",
                        "creationTimestamp": "2026-10-07T23:23:12-07:00",
                        "status": "READY",
                        "diskSizeGb": "100",
                    }
                ],
            },
        )

    monkeypatch.setattr(
        service.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    result = await service.newest_snapshot()
    assert result is not None and result.name == "newest"
    assert len(seen) == 2


async def test_snapshot_reader_rejects_repeated_pagination_token(monkeypatch):
    real_client = httpx.AsyncClient

    def respond(request):
        if request.url.host == "metadata.google.internal":
            return httpx.Response(200, json={"access_token": "synthetic-token"})
        return httpx.Response(200, json={"items": [], "nextPageToken": "repeated"})

    monkeypatch.setattr(
        service.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    with pytest.raises(ValueError, match="pagination incomplete"):
        await service.newest_snapshot()


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


async def test_provider_failure_is_explicit_and_never_leaks(monkeypatch, caplog):
    # The PostgreSQL harness may disable already-imported loggers during setup.
    monkeypatch.setattr(service._LOGGER, "disabled", False)
    caplog.set_level("WARNING", logger=service._LOGGER.name)
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
    assert "database backup status unavailable: RuntimeError" in caplog.text
    assert "database snapshot status unavailable: RuntimeError" in caplog.text
    assert "secret" not in caplog.text


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


def test_metadata_clock_skew_is_bounded_to_five_minutes():
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    source = now + timedelta(minutes=4)
    key = source.strftime("daily/%Y/%m/%d/manifest-%Y%m%dT%H%M%SZ.json")
    assert service.manifest_age(manifest(source), now, key) == 0
    with pytest.raises(ValueError):
        service.manifest_age(manifest(source), now - timedelta(seconds=1), key)


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


@pytest.mark.parametrize("bound", [None, 10])
async def test_missing_metadata_uses_the_same_exception_with_or_without_bound(bound):
    client = object.__new__(HippiusClient)
    client._presign = AsyncMock(return_value="https://test.invalid/synthetic")
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(404))
    )
    try:
        with pytest.raises(ObjectNotFoundError):
            await client.get_object(key="manifest.json", max_bytes=bound)
    finally:
        await client.aclose()


@pytest.mark.parametrize("failure", [503, 402, "network"])
async def test_bounded_metadata_retries_transient_failures(monkeypatch, failure):
    from ditto.api_server import hippius

    monkeypatch.setattr(hippius, "_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(hippius.asyncio, "sleep", AsyncMock())
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "network":
                raise httpx.ConnectError("synthetic", request=request)
            return httpx.Response(
                failure, content=b"UploadNotPermitted: failed to fetch billing balance"
            )
        return httpx.Response(200, content=b"{}")

    client = object.__new__(HippiusClient)
    client._presign = AsyncMock(return_value="https://test.invalid/synthetic")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        assert await client.get_object(key="manifest.json", max_bytes=10) == b"{}"
        assert calls == 2
    finally:
        await client.aclose()


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
