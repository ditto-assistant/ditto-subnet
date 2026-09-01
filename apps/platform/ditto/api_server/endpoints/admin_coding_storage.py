"""Admin-only, redacted Coding storage runtime readiness."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ditto.api_models.coding_storage_readiness import (
    AdminCodingStorageReadinessResponse,
)
from ditto.api_server.coding_storage_readiness import CodingStorageReadinessProbe
from ditto.api_server.endpoints.admin_quarantine import require_admin

router = APIRouter(prefix="/admin/coding-storage", tags=["admin"])
AdminDep = Annotated[None, Depends(require_admin)]


@router.get("/readiness", response_model=AdminCodingStorageReadinessResponse)
async def get_coding_storage_readiness(
    request: Request,
    response: Response,
    _admin: AdminDep,
) -> AdminCodingStorageReadinessResponse:
    response.headers["Cache-Control"] = "no-store"
    probe: CodingStorageReadinessProbe | None = getattr(
        request.app.state, "coding_storage_readiness_probe", None
    )
    if probe is None:
        raise HTTPException(
            status_code=503,
            detail="coding storage readiness is disabled",
            headers={"Cache-Control": "no-store"},
        )
    return await probe.snapshot()
