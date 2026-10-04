"""Authenticated metadata-only database recovery status."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from ditto.api_models.database_backup import DatabaseBackupStatus
from ditto.api_server.database_backup import backup_status
from ditto.api_server.endpoints.admin_quarantine import require_admin

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/database-backup-status", response_model=DatabaseBackupStatus)
async def get_database_backup_status(
    _admin: Annotated[None, Depends(require_admin)],
    response: Response,
) -> DatabaseBackupStatus:
    response.headers["Cache-Control"] = "no-store"
    return await backup_status()
