"""Metadata-only recovery observation using a separate read-only Hippius token."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime, timedelta

import httpx

from ditto.api_models.database_backup import (
    DatabaseBackupManifest,
    DatabaseBackupObject,
    DatabaseBackupStatus,
    DatabaseSnapshot,
)
from ditto.api_server.hippius import HippiusClient, HippiusConfig, ObjectSummary

_BUCKET = "ditto-platform-pg-backups"
_MANIFEST_KEY = re.compile(
    r"^daily/(\d{4})/(\d{2})/(\d{2})/manifest-(\d{8}T\d{6}Z)\.json$"
)
_OBJECT_KEY = re.compile(
    r"^(daily|monthly)/\d{4}/\d{2}/\d{2}/"
    r"(ditto_platform_prod-\d{8}T\d{6}Z\.dump\.age|"
    r"globals-\d{8}T\d{6}Z\.sql\.age|manifest-\d{8}T\d{6}Z\.json)$"
)
_PROJECT = "ditto-app-dev"
_DISK = (
    f"https://www.googleapis.com/compute/v1/projects/{_PROJECT}"
    "/zones/us-central1-a/disks/ditto-pg-platform"
)


def parse_config() -> HippiusConfig | None:
    access = os.environ.get("DATABASE_BACKUP_READER_ACCESS_KEY_ID", "").strip()
    secret = os.environ.get("DATABASE_BACKUP_READER_SECRET_ACCESS_KEY", "").strip()
    if not access and not secret:
        return None
    if not access or not secret:
        raise ValueError("incomplete database backup reader")
    return HippiusConfig(
        endpoint_url="https://s3.hippius.com",
        bucket=_BUCKET,
        access_key=access,
        secret_key=secret,
    )


async def inventory(client: HippiusClient, prefix: str) -> list[ObjectSummary]:
    rows: list[ObjectSummary] = []
    token = None
    seen: set[str] = set()
    for _ in range(10):
        page, token = await client.list_objects(
            prefix=prefix,
            max_keys=1000,
            continuation_token=token,
        )
        rows.extend(row for row in page if _OBJECT_KEY.fullmatch(row.key))
        if token is None:
            return rows
        if token in seen:
            raise ValueError("backup inventory pagination incomplete")
        seen.add(token)
    raise ValueError("backup inventory exceeded bound")


def manifest_age(manifest: DatabaseBackupManifest, now: datetime, key: str) -> float:
    match = _MANIFEST_KEY.fullmatch(key)
    if not match:
        raise ValueError("invalid backup manifest key")
    year, month, day, stamp = match.groups()
    started, completed = manifest.started_at, manifest.completed_at
    if started.tzinfo is None or completed.tzinfo is None:
        raise ValueError("backup timestamps require timezone")
    if (
        started.strftime("%Y%m%dT%H%M%SZ") != stamp
        or started.strftime("%Y/%m/%d") != f"{year}/{month}/{day}"
    ):
        raise ValueError("backup manifest identity differs")
    if (
        completed < started
        or completed > now + timedelta(minutes=5)
        or started > now + timedelta(minutes=5)
    ):
        raise ValueError("invalid backup timestamps")
    expected = {f"ditto_platform_prod-{stamp}.dump.age", f"globals-{stamp}.sql.age"}
    if len(manifest.objects) != 2 or {row.name for row in manifest.objects} != expected:
        raise ValueError("invalid encrypted backup object set")
    if any(not re.fullmatch(r"[0-9a-f]{64}", row.sha256) for row in manifest.objects):
        raise ValueError("invalid backup digests")
    if set(manifest.row_counts) != {"agents", "screening_attempts", "scores"}:
        raise ValueError("invalid backup row count set")
    if any(count <= 0 for count in manifest.row_counts.values()):
        raise ValueError("empty backup core tables")
    return max(0, (now - started).total_seconds() / 3600)


async def read_backups(status: DatabaseBackupStatus) -> None:
    config = parse_config()
    if config is None:
        status.backup_status = "disabled"
        return
    async with HippiusClient(config) as client:
        daily, monthly = await asyncio.gather(
            inventory(client, "daily/"),
            inventory(client, "monthly/"),
        )

        def summaries(rows: list[ObjectSummary]) -> list[DatabaseBackupObject]:
            return [
                DatabaseBackupObject(
                    key=row.key,
                    size=row.size,
                    last_modified=row.last_modified,
                )
                for row in sorted(rows, key=lambda row: row.key, reverse=True)[:12]
            ]

        status.daily = summaries(daily)
        status.monthly = summaries(monthly)
        markers = [row for row in daily if _MANIFEST_KEY.fullmatch(row.key)]
        if not markers:
            status.backup_status = "missing"
            return
        newest = max(markers, key=lambda row: row.key)
        if newest.size > 65536:
            raise ValueError("backup manifest exceeded bound")
        manifest = DatabaseBackupManifest.model_validate_json(
            await client.get_object(key=newest.key, max_bytes=65536),
        )
        hours = manifest_age(manifest, status.observed_at, newest.key)
        prefix = newest.key.rsplit("/", 1)[0] + "/"
        sizes = {row.key: row.size for row in daily}
        if any(sizes.get(prefix + row.name) != row.size for row in manifest.objects):
            raise ValueError("backup commit is missing encrypted objects")
        status.manifest = manifest
        status.hours_since_last_success = max(
            0, (status.observed_at - manifest.completed_at).total_seconds() / 3600
        )
        # Freshness is the snapshot's recovery point; a slow upload cannot make
        # an old database snapshot fresh merely by completing recently.
        status.backup_status = "fresh" if hours <= 36 else "stale"


async def newest_snapshot() -> DatabaseSnapshot | None:
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        identity = await client.get(
            "http://metadata.google.internal/computeMetadata/v1/"
            "instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"},
        )
        identity.raise_for_status()
        response = await client.get(
            f"https://compute.googleapis.com/compute/v1/projects/{_PROJECT}/global/snapshots",
            headers={"Authorization": f"Bearer {identity.json()['access_token']}"},
            params={
                "filter": f'sourceDisk = "{_DISK}"',
                "orderBy": "creationTimestamp desc",
                "maxResults": 1,
                "fields": "items(name,creationTimestamp,status,diskSizeGb)",
            },
        )
        response.raise_for_status()
        rows = response.json().get("items", [])
        if not rows:
            return None
        row = rows[0]
        return DatabaseSnapshot(
            name=row["name"],
            created_at=row["creationTimestamp"],
            status=row["status"],
            disk_size_gb=int(row["diskSizeGb"]),
        )


async def backup_status() -> DatabaseBackupStatus:
    status = DatabaseBackupStatus(
        observed_at=datetime.now(UTC),
        backup_status="unavailable",
        snapshot_status="unavailable",
    )
    try:
        await asyncio.wait_for(read_backups(status), timeout=30)
    except Exception:
        # No provider error/URL/credential bytes reach the wire.
        status.backup_status = "unavailable"
        status.manifest = None
        status.hours_since_last_success = None
    try:
        status.newest_snapshot = await newest_snapshot()
        status.snapshot_status = "present" if status.newest_snapshot else "missing"
    except Exception:
        status.snapshot_status = "unavailable"
    return status
