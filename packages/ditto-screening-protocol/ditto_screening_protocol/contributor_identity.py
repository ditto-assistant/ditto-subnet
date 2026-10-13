"""Versioned contributor identity and consent wire contracts and signing bytes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Address = Annotated[str, Field(pattern=r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Signature = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{128}$")]
GithubLogin = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$", max_length=39),
]
KeyKind = Literal["hotkey", "coldkey"]
ClaMode = Literal["off", "shadow", "enforce"]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class ContributorProof(WireModel):
    key_kind: KeyKind
    signer: Address
    signature: Signature


class GithubLinkRequest(WireModel):
    netuid: Annotated[int, Field(ge=0)]
    hotkey: Address
    github_user_id: Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]
    github_login: GithubLogin
    link_attempt_id: UUID
    nonce: UUID
    issued_at: AwareDatetime
    proof: ContributorProof


class GithubUnlinkRequest(WireModel):
    netuid: Annotated[int, Field(ge=0)]
    hotkey: Address
    link_id: UUID
    nonce: UUID
    issued_at: AwareDatetime
    proof: ContributorProof


class GithubLinkView(WireModel):
    link_id: UUID
    netuid: int
    hotkey: str
    coldkey: str
    github_user_id: int
    github_login: str
    link_attempt_id: UUID
    key_kind: KeyKind
    signer: str
    signature: str
    nonce: UUID
    issued_at: datetime
    linked_at: datetime
    revoked_at: datetime | None = None
    owner_block: int
    owner_block_hash: str


class GithubLinkStatus(WireModel):
    netuid: int
    enabled: bool
    link: GithubLinkView | None = None


class GithubLinkStart(WireModel):
    netuid: int
    attempt_id: UUID
    authorize_url: str
    expires_in: int


class GithubLinkAttemptStatus(WireModel):
    attempt_id: UUID
    status: Literal[
        "pending", "identity_verified", "accepted", "linked", "failed", "expired"
    ]
    github_user_id: int | None = None
    github_login: str | None = None
    error: str | None = None


class ClaVersionView(WireModel):
    version: int
    sha256: str
    published_at: datetime
    effective_at: datetime


class ClaSignRequest(WireModel):
    netuid: Annotated[int, Field(ge=0)]
    hotkey: Address
    cla_version: Annotated[int, Field(strict=True, ge=1, le=2147483647)]
    cla_sha256: Digest
    nonce: UUID
    issued_at: AwareDatetime
    signature: Signature


class ClaSignatureView(WireModel):
    signature_id: UUID
    netuid: int
    hotkey: str
    coldkey: str
    cla_version: int
    cla_sha256: str
    nonce: UUID
    issued_at: datetime
    signature: str
    signed_at: datetime
    owner_block: int
    owner_block_hash: str


class ClaStatus(WireModel):
    netuid: int
    mode: ClaMode
    current: ClaVersionView | None = None
    signature: ClaSignatureView | None = None
    is_current: bool = False


class ClaSettingsRevision(WireModel):
    revision: int
    parent_revision: int
    mode: ClaMode
    reason: str
    actor: str
    created_at: datetime


class ClaControl(WireModel):
    current: ClaSettingsRevision
    history: list[ClaSettingsRevision]
    effective_version: ClaVersionView | None
    active_hotkeys: int
    signed_active_hotkeys: int
    unsigned_active_hotkeys: int
    active_hotkeys_basis: Literal["non_retired_submissions"] = "non_retired_submissions"
    unsigned_shadow_uploads: int
    history_total: int
    history_limit: int
    history_offset: int
    history_has_more: bool


class ClaSettingsRequest(WireModel):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    mode: ClaMode
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class ClaPublishRequest(WireModel):
    version: Annotated[int, Field(strict=True, ge=1, le=2147483647)]
    sha256: Digest
    text: Annotated[str, Field(min_length=1, max_length=262144)]
    effective_at: AwareDatetime
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


def github_link_message(
    *,
    netuid: int,
    hotkey: str,
    github_user_id: int,
    github_login: str,
    link_attempt_id: UUID,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    issued = issued_at.astimezone(UTC).isoformat(timespec="microseconds")
    return (
        f"ditto-github-link:v1:{netuid}:{hotkey}:{github_user_id}:{github_login}:{link_attempt_id}:{nonce}:{issued}:{key_kind}:{signer}"
    ).encode()


def github_unlink_message(
    *,
    netuid: int,
    hotkey: str,
    link_id: UUID,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    issued = issued_at.astimezone(UTC).isoformat(timespec="microseconds")
    return (
        f"ditto-github-unlink:v1:{netuid}:{hotkey}:{link_id}:{nonce}:{issued}:{key_kind}:{signer}"
    ).encode()


def cla_message(
    *,
    netuid: int,
    cla_version: int,
    cla_sha256: str,
    hotkey: str,
    nonce: UUID,
    issued_at: datetime,
) -> bytes:
    issued = issued_at.astimezone(UTC).isoformat(timespec="microseconds")
    return (
        f"ditto-cla:v1:{netuid}:{cla_version}:{cla_sha256}:{hotkey}:{nonce}:{issued}"
    ).encode()
