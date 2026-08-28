"""Audited Backroom control for hotkey-level submission bans."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class AdminActiveHotkeyBan(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    hotkey: str
    reason: str | None
    banned_at: datetime


class AdminHotkeyBanAuditEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    seq: int
    hotkey: str
    action: Literal["unban"]
    actor: str
    reason: str
    previous_reason: str | None
    previous_banned_at: datetime
    recorded_at: datetime


class AdminHotkeyBanControl(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    hotkey: str
    banned: bool
    active_ban: AdminActiveHotkeyBan | None
    history: list[AdminHotkeyBanAuditEntry]


class AdminHotkeyBanList(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    total: int
    bans: list[AdminActiveHotkeyBan]


class AdminHotkeyUnbanRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    expected_banned_at: datetime
    reason: Annotated[str, Field(min_length=8)]
    confirmation: str


class AdminHotkeyUnbanResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    hotkey: str
    banned: Literal[False]
    action: AdminHotkeyBanAuditEntry
