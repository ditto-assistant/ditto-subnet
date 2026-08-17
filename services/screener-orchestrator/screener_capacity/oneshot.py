"""One-shot Targon rental teardown and leftover sweep.

Builder, runtime-smoke, source-review, and operator probes all create disposable
rentals. Live Targon DELETE returns HTTP 500 for suspended, error, and
registered records, so leftovers in those states are redeployed and then
deleted. Screener slot rentals are never touched.
"""

from __future__ import annotations

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


def _try_delete(client: TargonClient, uid: str) -> bool:
    try:
        client.delete(uid)
        return True
    except TargonAPIError:
        try:
            return str(client.state(uid).get("status", "")).casefold() == "deleted"
        except TargonAPIError:
            return False


def delete_oneshot_rental(client: TargonClient, uid: str) -> bool:
    """Delete a disposable rental. Never suspend as a teardown fallback.

    Live Targon DELETE returns HTTP 500 for `suspended`, `error`, and
    `registered` records. Redeploy so the provider has a live runtime, then
    DELETE immediately. Soft-deleted state counts as success.
    """
    if _try_delete(client, uid):
        return True
    try:
        status = str(client.state(uid).get("status", "")).casefold()
    except TargonAPIError:
        return False
    if status == "deleted":
        return True
    if status in {"suspended", "error", "registered"}:
        try:
            client.deploy(uid)
        except TargonAPIError:
            return False
        return _try_delete(client, uid)
    return _try_delete(client, uid)


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
        workers = max(1, max_workers)

        def _delete(row: dict[str, str | None]) -> dict[str, str | None]:
            uid = row["uid"] or ""
            action = (
                "deleted" if delete_oneshot_rental(client, uid) else "cleanup-required"
            )
            return {**row, "action": action}

        if workers == 1:
            results = [_delete(row) for row in pending]
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
