"""Read-only database recovery metadata; no credentials or database contents."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class DatabaseBackupObject(BaseModel):
    key: str
    size: int = Field(ge=0)
    last_modified: str


class DatabaseBackupManifestObject(BaseModel):
    name: str
    sha256: str
    size: int = Field(ge=22)


class DatabaseBackupManifest(BaseModel):
    format_version: Literal[1]
    database: Literal["ditto_platform_prod"]
    server_version: str
    server_version_num: int
    pg_dump_version: str
    database_bytes: int = Field(ge=0)
    alembic_version: str
    row_counts: dict[str, int]
    started_at: datetime
    completed_at: datetime
    objects: list[DatabaseBackupManifestObject]


class DatabaseSnapshot(BaseModel):
    name: str
    created_at: datetime
    status: str
    disk_size_gb: int


class DatabaseBackupStatus(BaseModel):
    observed_at: datetime
    bucket: Literal["ditto-platform-pg-backups"] = "ditto-platform-pg-backups"
    backup_status: Literal["disabled", "unavailable", "missing", "stale", "fresh"]
    daily: list[DatabaseBackupObject] = Field(default_factory=list)
    monthly: list[DatabaseBackupObject] = Field(default_factory=list)
    manifest: DatabaseBackupManifest | None = None
    hours_since_last_success: float | None = None
    snapshot_status: Literal["unavailable", "missing", "present"]
    newest_snapshot: DatabaseSnapshot | None = None
