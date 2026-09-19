"""World-readable administrative activity, without operator identities."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class PublicAdminActivity(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    recorded_at: datetime
    action: str
    method: str
    status: Literal["succeeded", "failed", "recorded", "unknown"]
    http_status: int | None
    details: dict
    source: str


class PublicAdminActivityPage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[PublicAdminActivity]
    next_before: int | None
