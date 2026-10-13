"""Platform-side GitHub OAuth callback, transient token scrubbing, and signed hotkey link store (#2871).

Byte-for-byte mirror of 'ditto/miner_cli/github_link.py'.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from uuid import UUID, uuid4

GITHUB_LINK_DOMAIN: Final = "ditto-github-link:v1"
GITHUB_UNLINK_DOMAIN: Final = "ditto-github-unlink:v1"
FRESHNESS_WINDOW: Final = timedelta(minutes=10)
KeyKind = Literal["hotkey", "coldkey"]


class GitHubLinkRejectedError(ValueError):
    """Raised when a GitHub link or unlink attestation is rejected."""


def _issued_stamp(issued_at: datetime) -> str:
    return issued_at.astimezone(UTC).isoformat(timespec="microseconds")


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
    issued = _issued_stamp(issued_at)
    return (
        f"{GITHUB_LINK_DOMAIN}:{netuid}:{hotkey}:{github_user_id}:{github_login}:"
        f"{link_attempt_id}:{nonce}:{issued}:{key_kind}:{signer}"
    ).encode("utf-8")


def github_unlink_message(
    *,
    netuid: int,
    hotkey: str,
    github_user_id: int,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    issued = _issued_stamp(issued_at)
    return (
        f"{GITHUB_UNLINK_DOMAIN}:{netuid}:{hotkey}:{github_user_id}:"
        f"{nonce}:{issued}:{key_kind}:{signer}"
    ).encode("utf-8")


@dataclass(frozen=True)
class VerifiedGitHubAttempt:
    attempt_id: UUID
    hotkey: str
    github_user_id: int
    github_login: str
    verified_at: datetime


@dataclass(frozen=True)
class MinerGitHubLinkRow:
    id: UUID
    hotkey: str
    coldkey: str
    github_user_id: int
    github_login: str
    key_kind: KeyKind
    signer: str
    signature: str
    nonce: UUID
    issued_at: datetime
    linked_at: datetime
    revoked_at: datetime | None = None


class GitHubLinkService:
    """Enforces OAuth token ephemerality, append-only link history, and active 1:1 uniqueness."""

    def __init__(self) -> None:
        self._attempts: dict[UUID, VerifiedGitHubAttempt] = {}
        self._rows: list[MinerGitHubLinkRow] = []
        self._seen_nonces: set[UUID] = set()

    def complete_oauth_callback(
        self,
        *,
        attempt_id: UUID,
        hotkey: str,
        oauth_code: str,
        exchange_code_fn,
        fetch_user_fn,
    ) -> dict:
        """Exchanges the OAuth code, reads numeric user id & login, and discards the access token immediately."""
        token_bundle = exchange_code_fn(oauth_code)
        access_token = token_bundle.get("access_token")
        try:
            profile = fetch_user_fn(access_token)
            github_user_id = int(profile["id"])
            github_login = str(profile["login"])
        finally:
            # Never persist, log, or return the GitHub access token
            access_token = None
            token_bundle.clear()

        attempt = VerifiedGitHubAttempt(
            attempt_id=attempt_id,
            hotkey=hotkey,
            github_user_id=github_user_id,
            github_login=github_login,
            verified_at=datetime.now(UTC),
        )
        self._attempts[attempt_id] = attempt
        return {
            "attempt_id": str(attempt_id),
            "hotkey": hotkey,
            "github_user_id": github_user_id,
            "github_login": github_login,
        }

    def finalize_signed_link(
        self,
        *,
        netuid: int,
        hotkey: str,
        link_attempt_id: UUID,
        key_kind: KeyKind,
        signer: str,
        signature_hex: str,
        nonce: UUID,
        issued_at: datetime,
        verify_fn,
        resolve_owner_coldkey_fn,
        now: datetime | None = None,
    ) -> MinerGitHubLinkRow:
        ref_now = now or datetime.now(UTC)
        if nonce in self._seen_nonces:
            raise GitHubLinkRejectedError("replayed nonce")
        if abs((ref_now - issued_at.astimezone(UTC)).total_seconds()) > FRESHNESS_WINDOW.total_seconds():
            raise GitHubLinkRejectedError("stale issued_at")

        attempt = self._attempts.get(link_attempt_id)
        if attempt is None or attempt.hotkey != hotkey:
            raise GitHubLinkRejectedError("unknown or mismatched link_attempt_id")

        coldkey = resolve_owner_coldkey_fn(hotkey)
        if not coldkey:
            raise GitHubLinkRejectedError("Owner(hotkey) could not be resolved")

        if key_kind == "hotkey" and signer != hotkey:
            raise GitHubLinkRejectedError("wrong signer: expected hotkey")
        if key_kind == "coldkey" and signer != coldkey:
            raise GitHubLinkRejectedError("wrong signer: expected owner coldkey")

        # Partial unique index check: is this numeric github_user_id already active on another hotkey?
        for row in self._rows:
            if row.revoked_at is None and row.github_user_id == attempt.github_user_id and row.hotkey != hotkey:
                raise GitHubLinkRejectedError(
                    f"github_user_id {attempt.github_user_id} is already linked to another active hotkey"
                )

        payload = github_link_message(
            netuid=netuid,
            hotkey=hotkey,
            github_user_id=attempt.github_user_id,
            github_login=attempt.github_login,
            link_attempt_id=link_attempt_id,
            nonce=nonce,
            issued_at=issued_at,
            key_kind=key_kind,
            signer=signer,
        )
        if not verify_fn(signer=signer, payload=payload, signature_hex=signature_hex):
            raise GitHubLinkRejectedError("signature verification failed")

        # If this hotkey had an existing active link, mark its revoked_at (history row preserved)
        for idx, row in enumerate(self._rows):
            if row.revoked_at is None and row.hotkey == hotkey:
                self._rows[idx] = replace(row, revoked_at=ref_now)

        self._seen_nonces.add(nonce)
        new_row = MinerGitHubLinkRow(
            id=uuid4(),
            hotkey=hotkey,
            coldkey=coldkey,
            github_user_id=attempt.github_user_id,
            github_login=attempt.github_login,
            key_kind=key_kind,
            signer=signer,
            signature=signature_hex,
            nonce=nonce,
            issued_at=issued_at.astimezone(UTC),
            linked_at=ref_now,
            revoked_at=None,
        )
        self._rows.append(new_row)
        return new_row

    def unlink(
        self,
        *,
        netuid: int,
        hotkey: str,
        key_kind: KeyKind,
        signer: str,
        signature_hex: str,
        nonce: UUID,
        issued_at: datetime,
        verify_fn,
        resolve_owner_coldkey_fn,
        now: datetime | None = None,
    ) -> MinerGitHubLinkRow:
        ref_now = now or datetime.now(UTC)
        if nonce in self._seen_nonces:
            raise GitHubLinkRejectedError("replayed nonce")
        active_idx = next(
            (i for i, r in enumerate(self._rows) if r.hotkey == hotkey and r.revoked_at is None),
            None,
        )
        if active_idx is None:
            raise GitHubLinkRejectedError("no active GitHub link for hotkey")
        active = self._rows[active_idx]
        coldkey = resolve_owner_coldkey_fn(hotkey)
        if (key_kind == "hotkey" and signer != hotkey) or (key_kind == "coldkey" and signer != coldkey):
            raise GitHubLinkRejectedError("wrong signer")

        payload = github_unlink_message(
            netuid=netuid,
            hotkey=hotkey,
            github_user_id=active.github_user_id,
            nonce=nonce,
            issued_at=issued_at,
            key_kind=key_kind,
            signer=signer,
        )
        if not verify_fn(signer=signer, payload=payload, signature_hex=signature_hex):
            raise GitHubLinkRejectedError("signature verification failed")

        self._seen_nonces.add(nonce)
        revoked = replace(active, revoked_at=ref_now)
        self._rows[active_idx] = revoked
        return revoked

    def public_read(self, hotkey: str) -> dict | None:
        for row in self._rows:
            if row.hotkey == hotkey and row.revoked_at is None:
                return {
                    "hotkey": row.hotkey,
                    "github_user_id": row.github_user_id,
                    "github_login": row.github_login,
                    "linked_at": row.linked_at.isoformat(),
                    "signature": row.signature,
                }
        return None

    def verify_bounty_claim_identity(self, *, hotkey: str, claimed_github_login: str) -> bool:
        active = self.public_read(hotkey)
        if active is None or active["github_login"].lower() != claimed_github_login.lower():
            raise GitHubLinkRejectedError(
                f"Bounty claim github_login '{claimed_github_login}' does not match active verified GitHub link for hotkey {hotkey}"
            )
        return True
