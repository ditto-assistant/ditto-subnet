"""Wire models for linking a miner hotkey to a Ditto account (Sign in with Ditto)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

DittoLinkClient = Literal["dashboard", "cli"]
DittoLinkAttemptStatus = Literal[
    "pending", "identity_verified", "authenticated", "linked", "failed", "expired"
]


class MinerDittoLinkView(BaseModel):
    """One hotkey's Ditto account link as the miner sees it.

    ``ditto_user_id`` is the verified OIDC subject; nothing here was supplied
    by the caller.
    """

    model_config = ConfigDict(extra="ignore")

    miner_hotkey: str
    ditto_user_id: str
    ditto_email: str | None = None
    miner_coldkey: str | None = None
    linked_via: DittoLinkClient
    created_at: datetime
    updated_at: datetime


class MinerDittoLinkResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool
    link: MinerDittoLinkView | None = None


class MinerDittoLinkStartRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    client: DittoLinkClient = "dashboard"
    return_to: str | None = Field(default=None, max_length=500)


class MinerDittoLinkStartResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    attempt_id: UUID
    authorize_url: str
    expires_in: int


class MinerDittoLinkAttemptResponse(BaseModel):
    """One attempt. ``identity_verified`` means Ditto signed someone in but
    that person has not yet accepted the hotkey; ``authenticated`` carries who
    accepted, so the miner can confirm the pairing before anything is written.
    Identity fields are only present once the Ditto side has accepted."""

    model_config = ConfigDict(extra="ignore")

    attempt_id: UUID
    status: DittoLinkAttemptStatus
    error: str | None = None
    ditto_user_id: str | None = None
    ditto_email: str | None = None
    miner_hotkey: str | None = None
    link: MinerDittoLinkView | None = None
