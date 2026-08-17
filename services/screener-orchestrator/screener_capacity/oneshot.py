"""One-shot Targon rental teardown and leftover sweep.

Targon bills a rental from create until DELETE, including `suspended` time.
DELETE of `suspended`/`error`/`registered` records returns HTTP 500, so those
leftovers are patched onto a public sleep image and briefly brought to
`running` only so DELETE can succeed. The original crashing container is
never resumed. Screener slot rentals are never touched.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any

from screener_capacity.targon import TargonAPIError, TargonClient, workload_summary

ONESHOT_NAME_PREFIXES = (
    "ditto-miner-build-",
    "ditto-build-",
    "ditto-runtime-",
    "ditto-source-",
    "ditto-rootless-probe-",
    "ditto-kaniko-probe-",
    "ditto-kaniko-runtime-",
    "ditto-agent-probe-",
    "ditto-buildkit-probe-",
    "ditto-dind-probe-",
    "ditto-sandbox-probe-",
    "ditto-screener-vm-probe-",
)
INFLIGHT_STATUSES = frozenset({"running", "provisioning"})
SWEEPABLE_STATUSES = frozenset({"suspended", "error", "registered", "deleted"})
DEFAULT_REGISTERED_GRACE_SECONDS = 1200
DEFAULT_INFLIGHT_GRACE_SECONDS = 2700
RUNNING_WAIT_SECONDS = 180.0
DELETED_SETTLE_SECONDS = 15.0
# Public image used only to make leftover DELETE legal. Never resume the
# original crashing job — that crash-loops and keeps the billing meter on.
HOLD_IMAGE = "busybox:1.37.0"
HOLD_COMMANDS = ["sleep", "3600"]


def is_oneshot_name(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in ONESHOT_NAME_PREFIXES)


def _created_age_seconds(created_at: str | None, *, now: datetime) -> float | None:
    if not created_at:
        return None
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (now - parsed.astimezone(UTC)).total_seconds()


def should_sweep(
    *,
    name: str,
    status: str | None,
    created_at: str | None,
    now: datetime,
    registered_grace_seconds: int,
    inflight_grace_seconds: int = DEFAULT_INFLIGHT_GRACE_SECONDS,
) -> bool:
    if not is_oneshot_name(name):
        return False
    normalized = (status or "").casefold()
    age = _created_age_seconds(created_at, now=now)
    if normalized in INFLIGHT_STATUSES:
        # created_at is the original record time. A leftover we redeployed
        # stays old even while provisioning; a live job is recent.
        return age is not None and age >= inflight_grace_seconds
    if normalized not in SWEEPABLE_STATUSES:
        return False
    if normalized != "registered":
        return True
    return age is not None and age >= registered_grace_seconds


def _current_status(client: TargonClient, uid: str) -> str:
    try:
        return str(client.state(uid).get("status", "")).casefold()
    except TargonAPIError:
        return ""


def _wait_until_running(client: TargonClient, uid: str) -> bool:
    deadline = time.monotonic() + RUNNING_WAIT_SECONDS
    while time.monotonic() < deadline:
        status = _current_status(client, uid)
        if status == "running":
            return True
        if status in {"deleted", "error", "suspended"}:
            return False
        time.sleep(5)
    return _current_status(client, uid) == "running"


def _try_delete(client: TargonClient, uid: str) -> bool:
    try:
        client.delete(uid)
        return True
    except TargonAPIError:
        try:
            return str(client.state(uid).get("status", "")).casefold() == "deleted"
        except TargonAPIError:
            return False


def _still_deleted(
    client: TargonClient,
    uid: str,
    *,
    settle_seconds: float | None = None,
) -> bool:
    """Targon can report `deleted` while stopping, then bounce to `error`."""
    wait = DELETED_SETTLE_SECONDS if settle_seconds is None else settle_seconds
    deadline = time.monotonic() + wait
    while True:
        status = _current_status(client, uid)
        if status not in {"", "deleted"}:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(5.0, remaining))


def _replace_with_hold_image(client: TargonClient, uid: str) -> bool:
    """Swap a leftover onto a cheap sleep image before waking it.

    PATCH of `suspended`/`registered` does not redeploy. PATCH of `error` or
    `running` can. Either way the next start must not be the old PID-1.
    """
    try:
        client.update(
            uid,
            image=HOLD_IMAGE,
            commands=HOLD_COMMANDS,
            args=[],
            envs=[],
        )
        return True
    except TargonAPIError:
        return False


def delete_oneshot_rental(client: TargonClient, uid: str) -> bool:
    """Delete a disposable rental. Suspended leftovers still accrue charges.

    Try DELETE first. If Targon 500s because the record is not `running`,
    replace the spec with a public sleep image and deploy only long enough
    for DELETE. Never treat suspend as terminal, and never resume the
    original crashing container.
    """
    if _try_delete(client, uid):
        return True
    status = _current_status(client, uid)
    if status == "deleted":
        return True
    if not _replace_with_hold_image(client, uid):
        return False
    status = _current_status(client, uid)
    if status in {"suspended", "error", "registered"}:
        try:
            client.deploy(uid)
        except TargonAPIError as error:
            if error.status == 402:
                raise
            status = _current_status(client, uid)
            if status not in {"running", "provisioning"}:
                return False
        else:
            status = _current_status(client, uid) or "provisioning"
    if status == "provisioning":
        if not _wait_until_running(client, uid):
            return False
        status = "running"
    if status == "running":
        if not _try_delete(client, uid):
            return False
        return _still_deleted(client, uid)
    return False


def sweep_oneshot_rentals(
    client: TargonClient,
    *,
    dry_run: bool = False,
    registered_grace_seconds: int = DEFAULT_REGISTERED_GRACE_SECONDS,
    now: datetime | None = None,
    max_workers: int = 1,
) -> dict[str, Any]:
    """Delete terminal one-shot rentals.

    Never mutates screener slots or inflight work.
    """
    moment = now or datetime.now(UTC)
    scanned = 0
    oneshot = 0
    skipped_inflight = 0
    skipped_grace = 0
    deleted = 0
    leftover = 0
    items: list[dict[str, str | None]] = []
    pending: list[dict[str, str | None]] = []
    for row in client.list_workloads():
        summary = workload_summary(row)
        scanned += 1
        if not is_oneshot_name(summary.name):
            continue
        oneshot += 1
        status = summary.status
        normalized = (status or "").casefold()
        if not should_sweep(
            name=summary.name,
            status=status,
            created_at=summary.created_at,
            now=moment,
            registered_grace_seconds=registered_grace_seconds,
        ):
            if normalized in INFLIGHT_STATUSES:
                skipped_inflight += 1
                action = "skipped-inflight"
            else:
                skipped_grace += 1
                action = "skipped-grace"
            items.append(
                {
                    "uid": summary.uid,
                    "name": summary.name,
                    "status": status,
                    "action": action,
                }
            )
            continue
        pending.append(
            {
                "uid": summary.uid,
                "name": summary.name,
                "status": status,
                "action": "would-delete" if dry_run else "pending",
            }
        )
    if dry_run:
        deleted = len(pending)
        items.extend(pending)
    elif pending:
        status_rank = {
            "suspended": 0,
            "error": 1,
            "registered": 2,
            "provisioning": 3,
            "running": 4,
        }
        pending.sort(
            key=lambda row: status_rank.get((row["status"] or "").casefold(), 5)
        )
        workers = max(1, max_workers)

        payment_blocked = False

        def _delete(row: dict[str, str | None]) -> dict[str, str | None]:
            nonlocal payment_blocked
            uid = row["uid"] or ""
            try:
                action = (
                    "deleted"
                    if delete_oneshot_rental(client, uid)
                    else "cleanup-required"
                )
            except TargonAPIError as error:
                if error.status == 402:
                    payment_blocked = True
                    action = "payment-required"
                else:
                    action = "cleanup-required"
            return {**row, "action": action}

        if workers == 1:
            results = []
            for row in pending:
                if payment_blocked and (row["status"] or "").casefold() in {
                    "suspended",
                    "error",
                    "registered",
                }:
                    results.append({**row, "action": "payment-required"})
                    continue
                results.append(_delete(row))
        else:
            results = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_delete, row) for row in pending]
                for future in as_completed(futures):
                    results.append(future.result())
        for row in results:
            if row["action"] == "deleted":
                deleted += 1
            else:
                leftover += 1
        items.extend(results)
    return {
        "phase": "oneshot-sweep",
        "dry_run": dry_run,
        "scanned": scanned,
        "oneshot": oneshot,
        "skipped_inflight": skipped_inflight,
        "skipped_grace": skipped_grace,
        "deleted": deleted,
        "leftover": leftover,
        "items": items,
    }
