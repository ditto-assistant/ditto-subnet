"""One-shot Targon rental teardown and leftover sweep.

Builder, runtime-smoke, source-review, and operator probes all create disposable
rentals. DELETE of a still-running rental can time out while Targon tears the
runtime down; the previous fallback suspended the record and never came back.
This module deletes first, suspends only to drop leftover runtime, retries
DELETE, and sweeps terminal leftovers. Screener slot rentals are never touched.
"""

from __future__ import annotations

import contextlib
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
    "ditto-agent-probe-",
    "ditto-buildkit-probe-",
    "ditto-dind-probe-",
    "ditto-screener-vm-probe-",
)
INFLIGHT_STATUSES = frozenset({"running", "provisioning"})
SWEEPABLE_STATUSES = frozenset({"suspended", "error", "registered", "deleted"})
DEFAULT_REGISTERED_GRACE_SECONDS = 1200


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
) -> bool:
    if not is_oneshot_name(name):
        return False
    normalized = (status or "").casefold()
    if normalized in INFLIGHT_STATUSES or normalized not in SWEEPABLE_STATUSES:
        return False
    if normalized != "registered":
        return True
    age = _created_age_seconds(created_at, now=now)
    return age is not None and age >= registered_grace_seconds


def delete_oneshot_rental(client: TargonClient, uid: str) -> bool:
    """Delete first; suspend only to drop runtime, then retry DELETE.

    A successful one-shot must not stay billed. Suspend is the zero-replica
    fallback, not the terminal record. Soft-deleted provider state counts as
    success so a lost DELETE response is not turned into a leftover.
    """
    try:
        client.delete(uid)
        return True
    except TargonAPIError:
        pass
    try:
        state = client.state(uid)
    except TargonAPIError:
        state = {}
    status = str(state.get("status", "")).casefold()
    if status == "deleted":
        return True
    if status != "suspended":
        with contextlib.suppress(TargonAPIError):
            client.suspend(uid)
    try:
        client.delete(uid)
        return True
    except TargonAPIError:
        try:
            status = str(client.state(uid).get("status", "")).casefold()
        except TargonAPIError:
            return False
        return status == "deleted"


def sweep_oneshot_rentals(
    client: TargonClient,
    *,
    dry_run: bool = False,
    registered_grace_seconds: int = DEFAULT_REGISTERED_GRACE_SECONDS,
    now: datetime | None = None,
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
        if dry_run:
            deleted += 1
            items.append(
                {
                    "uid": summary.uid,
                    "name": summary.name,
                    "status": status,
                    "action": "would-delete",
                }
            )
            continue
        if delete_oneshot_rental(client, summary.uid):
            deleted += 1
            action = "deleted"
        else:
            leftover += 1
            action = "cleanup-required"
        items.append(
            {
                "uid": summary.uid,
                "name": summary.name,
                "status": status,
                "action": action,
            }
        )
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
