"""Daily GM credit threshold and idempotent top-up request creation.

This scheduler has a GM read key but no treasury signing key. It creates a
bounded request when balance falls below the floor; the signer requires a
fresh quote, current Billing instructions, and independent plan approval.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ditto.treasury.gm import CreditBalance


@dataclass(frozen=True)
class DailyPolicy:
    gm_account_ref: str
    route: str
    floor_nano_usd: int
    target_nano_usd: int
    source_alpha_rao: int
    max_source_alpha_rao: int

    def validate(self) -> None:
        if not self.gm_account_ref or self.route not in ("tao", "gm_alpha"):
            raise ValueError("account and supported GM route are required")
        if not 0 < self.floor_nano_usd < self.target_nano_usd:
            raise ValueError("credit target must exceed positive floor")
        if not 0 < self.source_alpha_rao <= self.max_source_alpha_rao <= 10_000_000_000:
            raise ValueError("daily source amount exceeds the 10 DITTO alpha ceiling")


def create_daily_request(
    outbox: Path, policy: DailyPolicy, balance: CreditBalance, *, now: datetime
) -> Path | None:
    policy.validate()
    if now.tzinfo is None:
        raise ValueError("UTC timestamp is required")
    if balance.nano_usd >= policy.floor_nano_usd:
        return None
    day = now.astimezone(UTC).date().isoformat()
    account_hash = hashlib.sha256(policy.gm_account_ref.encode()).hexdigest()[:12]
    key = f"gm-{day}-{account_hash}"
    body = {
        "idempotency_key": key,
        "day": day,
        "observed_at": now.astimezone(UTC).isoformat(),
        "gm_account_ref": policy.gm_account_ref,
        "balance_nano_usd": balance.nano_usd,
        "floor_nano_usd": policy.floor_nano_usd,
        "target_nano_usd": policy.target_nano_usd,
        "route": policy.route,
        "source_alpha_rao": policy.source_alpha_rao,
        "max_source_alpha_rao": policy.max_source_alpha_rao,
        "state": "needs_current_billing_instructions_and_quote",
    }
    outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = outbox / f"{key}.json"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError as exc:
        if path.is_symlink():
            raise ValueError("daily request path may not be a symlink") from exc
        existing = json.loads(path.read_text())
        if (
            existing["idempotency_key"] != key
            or existing["gm_account_ref"] != policy.gm_account_ref
        ):
            raise ValueError(
                "daily request conflicts with existing outbox record"
            ) from exc
        return path
    try:
        os.write(fd, (json.dumps(body, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(outbox, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return path
