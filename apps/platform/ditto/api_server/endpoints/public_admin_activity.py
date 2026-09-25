"""Searchable, cursor-paginated public administrative history."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.admin_activity import PublicAdminActivity, PublicAdminActivityPage
from ditto.api_server.admin_activity import public_details
from ditto.api_server.dependencies import get_session
from ditto.db.models import AdminActivity, AdminActivityOutcome

router = APIRouter(prefix="/public/admin-activity", tags=["public"])


@router.get("", response_model=PublicAdminActivityPage)
async def list_activity(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    q: Annotated[str, Query(max_length=120)] = "",
    status: Literal["succeeded", "failed", "recorded", "unknown"] | None = None,
    before: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> PublicAdminActivityPage:
    outcome = func.coalesce(AdminActivityOutcome.status, "unknown")
    statement = select(AdminActivity, AdminActivityOutcome).outerjoin(
        AdminActivityOutcome, AdminActivityOutcome.activity_id == AdminActivity.id
    )
    if before is not None:
        statement = statement.where(AdminActivity.id < before)
    if status is not None:
        statement = statement.where(outcome == status)
    if q.strip():
        # Search only public columns; private actors and historical raw settings are
        # deliberately excluded so even counts/empty results cannot disclose them.
        needle = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(
            or_(
                func.replace(
                    func.replace(AdminActivity.action, "-", " "), "_", " "
                ).ilike(f"%{needle.replace(chr(45), chr(32))}%", escape="\\"),
                cast(AdminActivity.id, String).ilike(f"%{needle}%", escape="\\"),
                *[
                    AdminActivity.details[key]
                    .as_string()
                    .ilike(f"%{needle}%", escape="\\")
                    for key in ("agent_id", "canary_id", "rollout_id")
                ],
            )
        )
    rows = (
        await session.execute(
            statement.order_by(AdminActivity.id.desc()).limit(limit + 1)
        )
    ).all()
    items = [
        PublicAdminActivity(
            id=row.id,
            recorded_at=row.recorded_at,
            action=row.action.removeprefix("/api/v1/admin/"),
            method=row.method,
            status=completion.status if completion else "unknown",
            http_status=completion.http_status if completion else None,
            details=public_details(row.action, row.details),
            source=row.source,
        )
        for row, completion in rows[:limit]
    ]
    response.headers["Cache-Control"] = "public, max-age=5"
    return PublicAdminActivityPage(
        items=items, next_before=items[-1].id if len(rows) > limit else None
    )
