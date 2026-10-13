"""Platform-side CLA verification, append-only registry, upload gate, and Backroom metrics.

Byte-for-byte mirror of 'ditto/miner_cli/cla.py'.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

CLA_DOMAIN: Final = "ditto-cla:v1"
FRESHNESS_WINDOW: Final = timedelta(minutes=10)
GateMode = Literal["off", "shadow", "enforce"]


class ClaRejectedError(ValueError):
    """Raised when a CLA signature fails validation or when the upload gate blocks a hotkey."""


def _issued_stamp(issued_at: datetime) -> str:
    return issued_at.astimezone(UTC).isoformat(timespec="microseconds")


def cla_message(
    *,
    netuid: int,
    cla_version: int,
    cla_sha256: str,
    hotkey: str,
    nonce: UUID,
    issued_at: datetime,
) -> bytes:
    issued = _issued_stamp(issued_at)
    return (
        f"{CLA_DOMAIN}:{netuid}:{cla_version}:{cla_sha256.lower()}:"
        f"{hotkey}:{nonce}:{issued}"
    ).encode("utf-8")


@dataclass(frozen=True)
class ClaVersionRecord:
    version: int
    sha256: str
    text: str
    published_at: datetime
    effective_at: datetime


@dataclass(frozen=True)
class ClaSignatureRecord:
    id: UUID
    hotkey: str
    coldkey: str
    cla_version: int
    cla_sha256: str
    signature: str
    nonce: UUID
    issued_at: datetime
    signed_at: datetime
    artifact_sha256: str | None = None


@dataclass(frozen=True)
class ClaGateHistoryEntry:
    id: UUID
    mode: GateMode
    changed_by: str
    changed_at: datetime


class AppendOnlyClaStore:
    """In-memory / repository abstraction enforcing append-only CLA signature storage."""

    def __init__(self) -> None:
        self._versions: dict[int, ClaVersionRecord] = {}
        self._signatures: list[ClaSignatureRecord] = []
        self._seen_nonces: set[UUID] = set()
        self._gate_mode: GateMode = "off"
        self._gate_history: list[ClaGateHistoryEntry] = [
            ClaGateHistoryEntry(
                id=uuid4(),
                mode="off",
                changed_by="system-init",
                changed_at=datetime.now(UTC),
            )
        ]
        self._shadow_unsigned_Upload_count: int = 0

    def register_version(
        self,
        *,
        version: int,
        text: str,
        published_at: datetime | None = None,
        effective_at: datetime | None = None,
    ) -> ClaVersionRecord:
        now = datetime.now(UTC)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        rec = ClaVersionRecord(
            version=version,
            sha256=digest,
            text=text,
            published_at=published_at or now,
            effective_at=effective_at or now,
        )
        self._versions[version] = rec
        return rec

    def current_effective_version(self, *, now: datetime | None = None) -> ClaVersionRecord:
        ref = now or datetime.now(UTC)
        effective = [v for v in self._versions.values() if v.effective_at <= ref]
        if not effective:
            raise ClaRejectedError("No effective CLA version registered")
        return max(effective, key=lambda v: v.version)

    def set_gate_mode(self, *, mode: GateMode, changed_by: str) -> None:
        if mode not in ("off", "shadow", "enforce"):
            raise ValueError(f"Invalid CLA gate mode: {mode!r}")
        self._gate_mode = mode
        self._gate_history.append(
            ClaGateHistoryEntry(
                id=uuid4(),
                mode=mode,
                changed_by=changed_by,
                changed_at=datetime.now(UTC),
            )
        )

    def update_signature(self, *args, **kwargs) -> None:
        raise PermissionError("cla_signatures is strictly append-only; updates are forbidden")

    def delete_signature(self, *args, **kwargs) -> None:
        raise PermissionError("cla_signatures is strictly append-only; deletes are forbidden")

    def record_signature(
        self,
        *,
        netuid: int,
        hotkey: str,
        signer: str,
        cla_version: int,
        cla_sha256: str,
        signature_hex: str,
        nonce: UUID,
        issued_at: datetime,
        verify_fn,
        resolve_owner_coldkey_fn,
        now: datetime | None = None,
        artifact_sha256: str | None = None,
    ) -> ClaSignatureRecord:
        ref_now = now or datetime.now(UTC)
        if signer != hotkey:
            raise ClaRejectedError("wrong signer: CLA must be signed directly by the hotkey")
        if nonce in self._seen_nonces:
            raise ClaRejectedError("replayed nonce: CLA signature nonce has already been used")
        if abs((ref_now - issued_at.astimezone(UTC)).total_seconds()) > FRESHNESS_WINDOW.total_seconds():
            raise ClaRejectedError("stale issued_at: signature falls outside the freshness window")

        ver = self._versions.get(cla_version)
        if ver is None:
            raise ClaRejectedError(f"unknown cla_version {cla_version}")
        if ver.sha256.lower() != cla_sha256.lower():
            raise ClaRejectedError("cla_sha256 digest mismatch against registered CLA version")

        payload = cla_message(
            netuid=netuid,
            cla_version=cla_version,
            cla_sha256=cla_sha256,
            hotkey=hotkey,
            nonce=nonce,
            issued_at=issued_at,
        )
        if not verify_fn(signer=hotkey, payload=payload, signature_hex=signature_hex):
            raise ClaRejectedError("signature verification failed")

        coldkey = resolve_owner_coldkey_fn(hotkey)
        if not coldkey:
            raise ClaRejectedError("unable to resolve Owner(hotkey) coldkey at finalized block")

        self._seen_nonces.add(nonce)
        rec = ClaSignatureRecord(
            id=uuid4(),
            hotkey=hotkey,
            coldkey=coldkey,
            cla_version=cla_version,
            cla_sha256=cla_sha256.lower(),
            signature=signature_hex,
            nonce=nonce,
            issued_at=issued_at.astimezone(UTC),
            signed_at=ref_now,
            artifact_sha256=artifact_sha256,
        )
        self._signatures.append(rec)
        return rec

    def public_status(self, hotkey: str, *, now: datetime | None = None) -> dict:
        curr = self.current_effective_version(now=now)
        matching = [s for s in self._signatures if s.hotkey == hotkey]
        if not matching:
            return {
                "hotkey": hotkey,
                "signed": False,
                "is_current": False,
                "current_effective_version": curr.version,
                "cla_version": None,
                "cla_sha256": None,
                "signed_at": None,
                "signature": None,
            }
        latest = matching[-1]
        return {
            "hotkey": hotkey,
            "signed": True,
            "is_current": latest.cla_version == curr.version and latest.cla_sha256 == curr.sha256,
            "current_effective_version": curr.version,
            "cla_version": latest.cla_version,
            "cla_sha256": latest.cla_sha256,
            "signed_at": latest.signed_at.isoformat(),
            "signature": latest.signature,
        }

    def check_upload_gate(self, hotkey: str, *, now: datetime | None = None) -> bool:
        if self._gate_mode == "off":
            return True
        status = self.public_status(hotkey, now=now)
        if status["is_current"]:
            return True
        if self._gate_mode == "shadow":
            self._shadow_unsigned_Upload_count += 1
            logger.warning("CLA shadow gate: unsigned or outdated hotkey=%s", hotkey)
            return True
        raise ClaRejectedError(
            f"Hotkey {hotkey} has not signed the currently effective CLA (v{status['current_effective_version']}). "
            "Run 'uv run ditto cla sign' before uploading."
        )

    def backroom_adoption_summary(self, active_hotkeys: list[str], *, now: datetime | None = None) -> dict:
        curr = self.current_effective_version(now=now)
        signed_current = {
            s.hotkey
            for s in self._signatures
            if s.cla_version == curr.version and s.cla_sha256 == curr.sha256
        }
        active_set = set(active_hotkeys)
        return {
            "gate_mode": self._gate_mode,
            "current_effective_version": curr.version,
            "signed_active_hotkeys": len(active_set & signed_current),
            "unsigned_active_hotkeys": len(active_set - signed_current),
            "shadow_unsigned_upload_attempts": self._shadow_unsigned_Upload_count,
            "history": [
                {
                    "mode": h.mode,
                    "changed_by": h.changed_by,
                    "changed_at": h.changed_at.isoformat(),
                }
                for h in self._gate_history
            ],
        }
