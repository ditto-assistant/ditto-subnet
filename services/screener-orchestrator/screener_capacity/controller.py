"""Fenced GCE outage and backlog-overflow capacity reconciler.

The audited Hetzner node handles normal screening. GCE stays at zero until that
node is unavailable or unclaimed demand exceeds its configured backlog
multiple. A GCE worker claims new work and never retries a terminal Hetzner
lane. Retired nested-Docker Targon workers are never recreated here.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4


class ControllerError(RuntimeError):
    """A redacted, operator-actionable reconciliation failure."""


@dataclass(frozen=True)
class Demand:
    runnable: int
    active: int
    desired: int


@dataclass(frozen=True)
class ProviderCounts:
    healthy: int = 0
    pending: int = 0
    draining: int = 0

    @property
    def supplied(self) -> int:
        return self.healthy + self.pending


@dataclass(frozen=True)
class OverflowPolicy:
    enabled: bool
    primary_node_id: str | None
    backlog_multiplier: int
    min_backlog: int
    max_instances: int


@dataclass(frozen=True)
class ProviderRouting:
    revision: int
    runtime_provider_priority: tuple[Literal["hetzner", "targon", "gcp"], ...]
    source_review_provider_priority: tuple[Literal["hetzner", "targon", "gcp"], ...]
    build_provider_priority: tuple[Literal["hetzner", "targon", "gcp"], ...]
    overflow: OverflowPolicy = OverflowPolicy(
        enabled=False,
        primary_node_id=None,
        backlog_multiplier=3,
        min_backlog=12,
        max_instances=6,
    )

    @property
    def gcp_first(self) -> bool:
        return any(
            priority and priority[0] == "gcp"
            for priority in (
                self.build_provider_priority,
                self.runtime_provider_priority,
                self.source_review_provider_priority,
            )
        )

    @property
    def hetzner_first(self) -> bool:
        return all(
            priority and priority[0] == "hetzner"
            for priority in (
                self.build_provider_priority,
                self.runtime_provider_priority,
                self.source_review_provider_priority,
            )
        )


@dataclass(frozen=True)
class NodeInventory:
    states: dict[str, dict[str, Any]]
    # None when Platform predates the count, so GCE attribution is unknown.
    legacy_gcp_running_attempts: int | None


def desired_slots(*, runnable: int, active: int, jobs_per_slot: int, cap: int) -> int:
    """Keep every active lease supplied and add bounded catch-up capacity."""
    if min(runnable, active, cap) < 0 or jobs_per_slot < 1:
        raise ValueError("capacity inputs are out of range")
    return min(cap, active + math.ceil(runnable / jobs_per_slot))


def gce_capacity_target(*, demand: int) -> int:
    """Return the GCE worker capacity required by screening demand."""
    return demand


def gce_overflow_target(
    *,
    demand: Demand,
    routing: ProviderRouting,
    primary_node: dict[str, Any] | None,
    jobs_per_slot: int,
    global_cap: int,
) -> tuple[int, str]:
    """Choose GCE only for an explicit GCP route, outage, or queue overflow.

    A primary whose admission is
    known to be closed (``admission_open`` false, or ``screening_concurrency ==
    0`` from a Platform that predates that field) is an operator closure: a
    global full stop that GCE never overflows, whatever the backlog,
    ``gce_overflow_enabled``, or the host's readiness and heartbeat. Only raising
    the primary's ``screening_concurrency`` to at least one reopens screening. A
    primary the inventory cannot vouch for -- a failed node read, an omitted
    primary row, or a row without its admission setting -- also fails closed,
    since the operator stop cannot be ruled out. Only a primary known to be open
    but unavailable is a host failure that overflows.
    """
    if jobs_per_slot < 1 or global_cap < 0:
        raise ValueError("capacity inputs are out of range")
    primary = primary_node or {}
    primary_ready = primary.get("status") == "active" and primary.get("ready") is True
    screening_concurrency = int(primary.get("screening_concurrency", 0))
    admission_open = primary.get("admission_open")
    if admission_open is None and "screening_concurrency" in primary:
        # Platform releases before admission_open still report concurrency.
        admission_open = screening_concurrency > 0
    if admission_open is False or (
        "screening_concurrency" in primary and screening_concurrency == 0
    ):
        # A known operator closure holds through any host health change, so a
        # failed heartbeat cannot reopen screening through GCE.
        return 0, "HETZNER_PRIMARY_ADMISSION_CLOSED"
    if admission_open is None:
        return 0, "HETZNER_PRIMARY_UNKNOWN"
    if routing.gcp_first:
        return min(global_cap, demand.desired), "GCP_SCREENERS_PRIORITIZED_BY_POLICY"
    if any(
        priority and priority[0] == "targon"
        for priority in (
            routing.build_provider_priority,
            routing.runtime_provider_priority,
            routing.source_review_provider_priority,
        )
    ):
        # A stale revision naming the retired provider still falls back to GCE,
        # but only behind the same operator stop: never for an unknown primary.
        return min(global_cap, demand.desired), "RETIRED_PROVIDER_ROUTING"
    policy = routing.overflow
    if not routing.hetzner_first or not policy.enabled:
        return 0, "GCE_OVERFLOW_DISABLED"
    cap = min(global_cap, policy.max_instances)
    if cap == 0:
        return 0, "GCE_OVERFLOW_CAPPED_AT_ZERO"
    if not primary_ready:
        return min(cap, demand.desired), "HETZNER_PRIMARY_UNAVAILABLE"
    threshold = max(
        policy.min_backlog,
        screening_concurrency * policy.backlog_multiplier,
    )
    if demand.runnable <= threshold:
        return 0, "HETZNER_PRIMARY_HANDLING_BASE_LOAD"
    overflow_jobs = demand.runnable - threshold
    return (
        min(cap, math.ceil(overflow_jobs / jobs_per_slot)),
        "HETZNER_BACKLOG_OVERFLOW",
    )


def _read_secret_file(path: Path) -> str:
    try:
        value = path.read_text().strip()
    except OSError as error:
        raise ControllerError(f"credential file is unavailable: {path.name}") from error
    if len(value) < 32:
        raise ControllerError(f"credential file is invalid: {path.name}")
    return value


def _source_sha() -> str:
    """Resolve the exact checked-out controller source without trusting argv."""

    repository = Path(__file__).resolve().parents[3]
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ControllerError("controller source revision is unavailable") from error
    revision = result.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ControllerError("controller source revision is invalid")
    return revision


def _json_request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    allow_not_found: bool = False,
) -> Any:
    headers = {"Accept": "application/json"}
    data = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404 and allow_not_found:
            return None
        if error.code == 409:
            raise ControllerError(
                "controller lease is held by another writer"
            ) from None
        raise ControllerError(
            f"Platform {method} failed with HTTP {error.code}"
        ) from None
    except (TimeoutError, urllib.error.URLError, OSError) as error:
        raise ControllerError(f"Platform {method} transport failed") from error
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise ControllerError("Platform returned invalid JSON") from error


def _parse_provider_routing(body: object) -> ProviderRouting:
    """Validate a provider-settings body from Platform or the state cache."""
    if not isinstance(body, dict):
        raise ControllerError("Platform provider settings response is invalid")
    revision = body.get("revision")
    values = body.get("settings")
    if not isinstance(revision, int) or revision < 0 or not isinstance(values, dict):
        raise ControllerError("Platform provider settings response is invalid")

    def priority(
        field: str,
    ) -> tuple[Literal["hetzner", "targon", "gcp"], ...]:
        raw = values.get(field)
        if (
            not isinstance(raw, list)
            or not raw
            or not all(
                isinstance(item, str) and item in {"hetzner", "targon", "gcp"}
                for item in raw
            )
            or len(raw) != len(set(raw))
            or "gcp" not in raw
        ):
            raise ControllerError("Platform provider priority is invalid")
        return cast(tuple[Literal["hetzner", "targon", "gcp"], ...], tuple(raw))

    try:
        overflow = OverflowPolicy(
            enabled=bool(values["gce_overflow_enabled"]),
            primary_node_id=(
                str(values["primary_node_id"])
                if values.get("primary_node_id") is not None
                else None
            ),
            backlog_multiplier=int(values["gce_overflow_backlog_multiplier"]),
            min_backlog=int(values["gce_overflow_min_backlog"]),
            max_instances=int(values["gce_overflow_max_instances"]),
        )
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ControllerError("Platform overflow settings are invalid") from error
    if (
        not 2 <= overflow.backlog_multiplier <= 20
        or not 1 <= overflow.min_backlog <= 1000
        or not 0 <= overflow.max_instances <= 32
        or (overflow.enabled and not overflow.primary_node_id)
    ):
        raise ControllerError("Platform overflow settings are invalid")

    return ProviderRouting(
        revision=revision,
        runtime_provider_priority=priority("runtime_provider_priority"),
        source_review_provider_priority=priority("source_review_provider_priority"),
        build_provider_priority=priority("build_provider_priority"),
        overflow=overflow,
    )


def _routing_to_state(routing: ProviderRouting) -> dict[str, Any]:
    """Serialize a routing revision in the Platform provider-settings shape."""
    return {
        "revision": routing.revision,
        "settings": {
            "runtime_provider_priority": list(routing.runtime_provider_priority),
            "source_review_provider_priority": list(
                routing.source_review_provider_priority
            ),
            "build_provider_priority": list(routing.build_provider_priority),
            "gce_overflow_enabled": routing.overflow.enabled,
            "primary_node_id": routing.overflow.primary_node_id,
            "gce_overflow_backlog_multiplier": routing.overflow.backlog_multiplier,
            "gce_overflow_min_backlog": routing.overflow.min_backlog,
            "gce_overflow_max_instances": routing.overflow.max_instances,
        },
    }


def _routing_from_state(value: object) -> ProviderRouting | None:
    """Rebuild the cached routing, or None when it is missing or malformed."""
    try:
        return _parse_provider_routing(value)
    except ControllerError:
        return None


class PlatformControl:
    def __init__(self, *, base_url: str, token: str, environment: str) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self.environment = environment

    def demand(self, *, jobs_per_slot: int, cap: int) -> Demand:
        body = _json_request("GET", f"{self._base}/api/v1/public/activity?limit=1")
        counts = body.get("status_counts", {}) if isinstance(body, dict) else {}
        runnable = int(counts.get("waiting_screening", 0))
        active = int(counts.get("screening", 0))
        return Demand(
            runnable=runnable,
            active=active,
            desired=desired_slots(
                runnable=runnable,
                active=active,
                jobs_per_slot=jobs_per_slot,
                cap=cap,
            ),
        )

    def renew(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        body = _json_request(
            "PUT",
            f"{self._base}/api/v1/screener/controller/capacity",
            token=self._token,
            payload=snapshot,
        )
        if not isinstance(body, dict):
            raise ControllerError("Platform capacity response is invalid")
        return body

    def provider_routing(self) -> ProviderRouting:
        return _parse_provider_routing(
            _json_request(
                "GET",
                f"{self._base}/api/v1/screener/controller/provider-settings"
                f"?environment={self.environment}",
                token=self._token,
            )
        )

    def fence(self, *, epoch: str) -> None:
        """Verify this writer still owns an unexpired lease without renewing it."""
        _json_request(
            "POST",
            f"{self._base}/api/v1/screener/controller/fence",
            token=self._token,
            payload={
                "environment": self.environment,
                "controller_epoch": epoch,
            },
        )

    def node_states(self) -> dict[str, dict[str, Any]]:
        return self.node_inventory().states

    def node_inventory(self) -> NodeInventory:
        body = _json_request(
            "GET",
            f"{self._base}/api/v1/screener/controller/nodes"
            f"?environment={self.environment}",
            token=self._token,
        )
        rows = body.get("nodes") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise ControllerError("Platform node readiness response is invalid")
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("node_id"), str)
                or not isinstance(row.get("provider_resource_id"), str)
            ):
                continue
            result[str(row["node_id"])] = row
            result[str(row["provider_resource_id"])] = row
        running = body.get("legacy_gcp_running_attempts")
        return NodeInventory(
            states=result,
            legacy_gcp_running_attempts=(
                running
                if isinstance(running, int)
                and not isinstance(running, bool)
                and running >= 0
                else None
            ),
        )


class GCEFleet:
    WATCHDOG_MODE = "ONLY_SCALE_OUT"

    def __init__(
        self,
        *,
        project: str,
        region: str,
        mig: str,
        impersonate_service_account: str | None = None,
    ) -> None:
        self.project = project
        self.region = region
        self.mig = mig
        self.impersonate_service_account = impersonate_service_account

    def _run(self, *arguments: str) -> str:
        command = ["gcloud", *arguments, "--project", self.project, "--quiet"]
        if self.impersonate_service_account:
            command.extend(
                [
                    "--impersonate-service-account",
                    self.impersonate_service_account,
                ]
            )
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ControllerError("GCE managed-group operation failed") from error
        return result.stdout

    def target(self) -> int:
        output = self._run(
            "compute",
            "instance-groups",
            "managed",
            "describe",
            self.mig,
            "--region",
            self.region,
            "--format=value(targetSize)",
        )
        try:
            return int(output.strip())
        except ValueError as error:
            raise ControllerError("GCE managed-group target is invalid") from error

    def counts(self) -> ProviderCounts:
        output = self._run(
            "compute",
            "instance-groups",
            "managed",
            "list-instances",
            self.mig,
            "--region",
            self.region,
            "--format=json(instanceStatus,currentAction)",
        )
        try:
            rows = json.loads(output)
        except json.JSONDecodeError as error:
            raise ControllerError("GCE instance list is invalid") from error
        healthy = pending = draining = 0
        for row in rows if isinstance(rows, list) else []:
            action = str(row.get("currentAction", "")).upper()
            status = str(row.get("instanceStatus", "")).upper()
            if action in {"DELETING", "ABANDONING", "RECREATING"}:
                draining += 1
            elif action in {"CREATING", "CREATING_WITHOUT_RETRIES", "VERIFYING"}:
                pending += 1
            elif status == "RUNNING" and action in {"NONE", ""}:
                healthy += 1
            else:
                pending += 1
        return ProviderCounts(healthy=healthy, pending=pending, draining=draining)

    def running_instances(self) -> set[str]:
        """Name the running instances the managed group is not already changing."""
        output = self._run(
            "compute",
            "instance-groups",
            "managed",
            "list-instances",
            self.mig,
            "--region",
            self.region,
            "--format=json(instance,instanceStatus,currentAction)",
        )
        try:
            rows = json.loads(output)
        except json.JSONDecodeError as error:
            raise ControllerError("GCE instance list is invalid") from error
        return {
            str(row.get("instance", "")).rsplit("/", 1)[-1]
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict)
            and str(row.get("instanceStatus", "")).upper() == "RUNNING"
            and str(row.get("currentAction", "")).upper() in {"NONE", ""}
        } - {""}

    def _autoscaler_mode(self) -> str:
        output = self._run(
            "compute",
            "instance-groups",
            "managed",
            "describe",
            self.mig,
            "--region",
            self.region,
            "--format=value(autoscaler.autoscalingPolicy.mode)",
        )
        mode = output.strip().upper()
        if not mode:
            raise ControllerError("GCE autoscaler mode is missing")
        return mode

    def _set_autoscaler_mode(self, mode: str) -> None:
        self._run(
            "compute",
            "instance-groups",
            "managed",
            "update-autoscaling",
            self.mig,
            "--region",
            self.region,
            "--mode",
            mode,
        )

    def ensure_watchdog(self) -> None:
        """Keep the independent safety net ready, including at zero capacity.

        The metric publishes zero while Platform's watchdog suppresses fallback.
        ONLY_SCALE_OUT cannot delete workers or race the controller's scale-in.
        """
        if self._autoscaler_mode() != self.WATCHDOG_MODE:
            self._set_autoscaler_mode("only-scale-out")

    def _paused_mutation(self, operation: str, *arguments: str) -> None:
        # Compute rejects manual resize while any autoscaler mode is active,
        # including ONLY_SCALE_OUT. Keep the emergency policy configured, pause
        # it only around the fenced mutation and always restore it. Platform's
        # policy-aware metric suppresses fallback at zero capacity.
        mutation_error: ControllerError | None = None
        try:
            self._set_autoscaler_mode("off")
            self._run(
                "compute",
                "instance-groups",
                "managed",
                operation,
                self.mig,
                "--region",
                self.region,
                *arguments,
            )
        except ControllerError as error:
            mutation_error = error
        try:
            self._set_autoscaler_mode("only-scale-out")
        except ControllerError as restore_error:
            raise ControllerError(
                "GCE autoscaler watchdog restore failed"
            ) from restore_error
        if mutation_error is not None:
            raise mutation_error

    def resize(self, target: int) -> None:
        self._paused_mutation("resize", "--size", str(target))

    def delete_instances(self, names: list[str]) -> None:
        """Delete named instances; the managed group shrinks by the same count."""
        self._paused_mutation(
            "delete-instances",
            f"--instances={','.join(names)}",
        )


class GCPBootstrapTokenMinter:
    """Mint a short-lived token without creating a service-account key."""

    def __init__(self, *, target: str, delegate: str | None) -> None:
        self.target = target
        self.delegate = delegate

    def mint(self) -> str:
        impersonation_chain = (
            f"{self.delegate},{self.target}" if self.delegate else self.target
        )
        command = [
            "gcloud",
            "auth",
            "print-access-token",
            f"--impersonate-service-account={impersonation_chain}",
            "--lifetime=1800",
            "--quiet",
        ]
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ControllerError("worker bootstrap token mint failed") from error
        token = result.stdout.strip()
        if len(token) < 100:
            raise ControllerError("worker bootstrap token mint returned invalid data")
        return token


def _load_state(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _provider_state(path: Path) -> tuple[bool, str | None, str | None]:
    state = _load_state(path)
    code = state.get("last_provider_error_code")
    error_at = state.get("last_provider_error_at")
    return (
        state.get("provider_ready") is True,
        code if isinstance(code, str) else None,
        error_at if isinstance(error_at, str) else None,
    )


def _persist_provider_state(
    path: Path,
    *,
    ready: bool,
    error_code: str | None,
    error_at: str | None,
) -> None:
    state = _load_state(path)
    state.update(
        {
            "provider_ready": ready,
            "last_provider_error_code": error_code,
            "last_provider_error_at": error_at,
        }
    )
    _write_state(path, state)


@dataclass(frozen=True)
class Settings:
    platform_url: str
    platform_token_file: Path
    environment: str
    epoch: str
    source_sha: str
    global_cap: int
    jobs_per_slot: int
    interval_seconds: int
    state_file: Path
    gce_project: str
    gce_region: str
    gce_mig: str
    gce_impersonate_service_account: str | None
    lock_file: Path
    dry_run: bool
    # Consecutive passes that keep the GCE target while a Platform routing or
    # node-inventory read fails, so a Platform deploy cannot flap the MIG.
    inventory_failure_hold_passes: int = 4


def _snapshot(
    *,
    settings: Settings,
    provider_routing: ProviderRouting,
    demand: Demand,
    reason: str | None,
    gce: ProviderCounts,
    gce_target: int,
    events: list[dict[str, Any]],
    provider_success_at: str | None,
    provider_error_code: str | None,
    provider_error_at: str | None,
    provider_ready: bool,
) -> dict[str, Any]:
    return {
        "environment": settings.environment,
        "controller_epoch": settings.epoch,
        "controller_source_sha": settings.source_sha,
        "provider_settings_revision": provider_routing.revision,
        "runnable_backlog": demand.runnable,
        "active_leases": demand.active,
        "desired_slots": demand.desired,
        "global_cap": settings.global_cap,
        "provider_ready": provider_ready,
        "targon_capability": "nogo",
        "targon_available": 0,
        "targon_healthy": 0,
        "targon_pending": 0,
        "targon_draining": 0,
        "gce_target": gce_target,
        "gce_healthy": gce.healthy,
        "gce_pending": gce.pending,
        "gce_draining": gce.draining,
        "fallback_reason": reason,
        "last_provider_success_at": provider_success_at,
        "last_provider_error_code": provider_error_code,
        "last_provider_error_at": provider_error_at,
        "events": events,
    }


def _record_provider_failure(
    platform: PlatformControl,
    snapshot: dict[str, Any],
    *,
    state_file: Path,
    code: str,
    detail: str,
) -> None:
    """Best-effort error heartbeat so a failed mutation cannot look ready."""
    failed = {
        **snapshot,
        "provider_ready": False,
        "last_provider_error_code": code,
        "last_provider_error_at": datetime.now(UTC).isoformat(),
        "events": [
            {
                "event_type": "provider_mutation_failed",
                "detail": detail,
            }
        ],
    }
    with contextlib.suppress(OSError):
        _persist_provider_state(
            state_file,
            ready=False,
            error_code=code,
            error_at=str(failed["last_provider_error_at"]),
        )
    with contextlib.suppress(ControllerError):
        platform.renew(failed)


def _plan_gce_scale_in(
    platform: PlatformControl,
    gce_fleet: GCEFleet,
    *,
    target: int,
    current_target: int,
    claims_fenced_at_zero: bool,
) -> tuple[list[str], str | None]:
    """Check whether scale-in is safe, or explain why it must wait.

    Runs after the fenced renew. A GCE worker may have claimed since the first
    inventory read, so leases are read again here. At zero a ready,
    Hetzner-primary route's renew withdrew overflow claims; a GCP-first route
    or an unready snapshot cannot safely fence them. Partial scale-in also
    waits for a per-instance claim fence, since an idle heartbeat is not one.
    """
    try:
        inventory = platform.node_inventory()
    except ControllerError:
        return [], "inventory_unavailable"
    rows = {
        str(row["node_id"]): row
        for row in inventory.states.values()
        if row.get("provider") == "gcp"
    }
    running = inventory.legacy_gcp_running_attempts
    if target == 0:
        if not claims_fenced_at_zero:
            return [], "legacy_claims_not_fenced"
        if running is None:
            return [], "attribution_incomplete"
        if running > 0 or any(row.get("active_lease") is True for row in rows.values()):
            return [], "gce_active_lease"
        members = gce_fleet.running_instances()
        if len(members) != current_target or any(
            (row := rows.get(member)) is None
            or row.get("ready") is not True
            or row.get("instance_busy") is not False
            for member in members
        ):
            return [], "instance_inventory_incomplete"
        return [], None
    legacy = [row for row in rows.values() if row.get("instance_busy") is not None]
    busy = sum(row["instance_busy"] is True for row in legacy)
    if running is None or running > busy:
        return [], "attribution_incomplete"
    # A heartbeat stays ready for minutes after its VM is deleted, so only
    # current managed-group members are candidates.
    members = gce_fleet.running_instances()
    idle = sorted(
        (
            row
            for row in legacy
            if row.get("ready") is True
            and row.get("instance_busy") is False
            and row["node_id"] in members
        ),
        key=lambda row: str(row.get("heartbeat_seen_at") or ""),
    )
    excess = current_target - target
    if len(idle) < excess:
        return [], "insufficient_idle_instances"
    # The shared legacy hotkey can claim on any instance while target is
    # nonzero. Deleting a merely idle instance can orphan a new lease.
    return [], "per_instance_claim_fence_unavailable"


def reconcile(settings: Settings) -> dict[str, Any]:
    token = _read_secret_file(settings.platform_token_file)
    platform = PlatformControl(
        base_url=settings.platform_url,
        token=token,
        environment=settings.environment,
    )
    demand = platform.demand(
        jobs_per_slot=settings.jobs_per_slot, cap=settings.global_cap
    )
    state = _load_state(settings.state_file)
    provider_routing_available = True
    cached_routing: ProviderRouting | None = None
    try:
        provider_routing = platform.provider_routing()
    except ControllerError:
        # Without the current routing revision we cannot prove that operator
        # admission is open. Keep existing capacity while recording the failure;
        # the policy-aware metric may activate fallback independently.
        provider_routing_available = False
        cached_routing = _routing_from_state(state.get("last_good_provider_routing"))
        provider_routing = cached_routing or ProviderRouting(
            revision=0,
            runtime_provider_priority=("hetzner", "gcp"),
            source_review_provider_priority=("hetzner", "gcp"),
            build_provider_priority=("hetzner", "gcp"),
        )
    node_states_available = True
    try:
        node_states_reader = getattr(platform, "node_states", None)
        if node_states_reader is None:
            node_states_available = False
            node_states = {}
        else:
            node_states = node_states_reader()
    except ControllerError:
        node_states_available = False
        node_states = {}
    failed_reads = [
        name
        for name, available in (
            ("routing", provider_routing_available),
            ("nodes", node_states_available),
        )
        if not available
    ]
    prior_failures = state.get("inventory_failures")
    if (
        not isinstance(prior_failures, int)
        or isinstance(prior_failures, bool)
        or prior_failures < 0
    ):
        prior_failures = 0
    inventory_failures = prior_failures + 1 if failed_reads else 0
    holding = bool(failed_reads) and (
        inventory_failures <= settings.inventory_failure_hold_passes
    )
    if not settings.dry_run:
        # Keep the last good routing even if a later provider read fails. The
        # failure count advances only once the first fenced renew delivers its
        # transition events.
        if provider_routing_available:
            state["last_good_provider_routing"] = _routing_to_state(provider_routing)
        _write_state(settings.state_file, state)
    provider_success_at: str | None = None
    provider_error_code: str | None = None
    provider_error_at: str | None = None
    if not provider_routing_available and cached_routing is None:
        provider_error_code = "PROVIDER_ROUTING_UNAVAILABLE"
        provider_error_at = datetime.now(UTC).isoformat()
    gce_fleet = GCEFleet(
        project=settings.gce_project,
        region=settings.gce_region,
        mig=settings.gce_mig,
        impersonate_service_account=settings.gce_impersonate_service_account,
    )
    current_target = gce_fleet.target()
    gce_counts = gce_fleet.counts()
    provider_success_at = datetime.now(UTC).isoformat()

    primary_node = node_states.get(provider_routing.overflow.primary_node_id or "")
    if provider_routing_available:
        target, reason = gce_overflow_target(
            demand=demand,
            routing=provider_routing,
            primary_node=primary_node,
            jobs_per_slot=settings.jobs_per_slot,
            global_cap=settings.global_cap,
        )
    else:
        # A cached revision can retain claim compatibility, but cannot
        # authorize a physical resize without a fresh policy read.
        target, reason = current_target, "PROVIDER_ROUTING_UNAVAILABLE"
    gce_has_active_lease = any(
        node.get("provider") == "gcp" and node.get("active_lease") is True
        for node in node_states.values()
    )
    if target < current_target and gce_has_active_lease:
        # Never remove GCE capacity while a GCE worker owns a live lease.
        target = current_target
    if target < current_target and not node_states_available and demand.active > 0:
        # A missing node inventory means we cannot prove that none of the live
        # attempts belongs to GCE. Keep current capacity until the authoritative
        # inventory returns instead of guessing during scale-in.
        target = current_target
    if holding:
        # A Platform deploy or transient read failure must not flap the MIG in
        # either direction. The hold never adds capacity, so a primary closure
        # already observed keeps GCE at zero; once the hold expires an unknown
        # primary fails closed as usual.
        target = current_target
        reason = "PLATFORM_INVENTORY_UNAVAILABLE"
    events: list[dict[str, Any]] = []
    if target != current_target:
        events.append(
            {
                "event_type": "gce_target_changed",
                "provider": "gcp",
                "detail": f"GCE target {current_target} -> {target}",
            }
        )
    failed_detail = f"{' and '.join(failed_reads)} read"
    if inventory_failures == 1:
        events.append(
            {
                "event_type": "platform_inventory_unavailable",
                "provider": "gcp",
                "detail": f"{failed_detail} failed"
                + (f"; holding GCE target {current_target}" if holding else ""),
            }
        )
    hold_passes = settings.inventory_failure_hold_passes
    if hold_passes > 0 and inventory_failures == hold_passes + 1:
        events.append(
            {
                "event_type": "platform_inventory_hold_expired",
                "provider": "gcp",
                "detail": (
                    f"{failed_detail} still failing after {hold_passes} held passes"
                ),
            }
        )
    last_reason = _load_state(settings.state_file).get("last_fallback_reason")
    if isinstance(last_reason, str) and last_reason != reason:
        events.append(
            {
                "event_type": "fallback_reason_changed",
                "provider": "hetzner",
                "detail": f"{last_reason} -> {reason}",
            }
        )
    prior_provider_ready, prior_error_code, prior_error_at = _provider_state(
        settings.state_file
    )
    starting_provider_ready = prior_provider_ready and provider_error_code is None
    starting_error_code = provider_error_code
    starting_error_at = provider_error_at
    if provider_error_code is None and not prior_provider_ready:
        starting_error_code = prior_error_code
        starting_error_at = prior_error_at
    snapshot = _snapshot(
        settings=settings,
        provider_routing=provider_routing,
        demand=demand,
        reason=reason,
        gce=gce_counts,
        gce_target=target,
        events=events,
        provider_success_at=provider_success_at,
        provider_error_code=starting_error_code,
        provider_error_at=starting_error_at,
        provider_ready=starting_provider_ready,
    )
    if settings.dry_run:
        return snapshot

    # Lease acquisition/renewal fences every mutation below.  A concurrent
    # epoch receives 409 while the existing lease remains live.
    platform.renew(snapshot)
    # The renewed snapshot delivered any reason-change event; record the
    # reason now so a later failed mutation cannot repeat the transition.
    state = _load_state(settings.state_file)
    state["inventory_failures"] = inventory_failures
    state["last_fallback_reason"] = reason
    _write_state(settings.state_file, state)
    if target == current_target:
        try:
            platform.fence(epoch=settings.epoch)
            gce_fleet.ensure_watchdog()
        except ControllerError:
            _record_provider_failure(
                platform,
                snapshot,
                state_file=settings.state_file,
                code="GCE_WATCHDOG_RESTORE_FAILED",
                detail="GCE emergency autoscaler restore failed",
            )
            raise
    if target > current_target:
        # Bring fallback capacity up before any later reconciliation work.
        try:
            platform.fence(epoch=settings.epoch)
            gce_fleet.resize(target)
            current_target = target
        except ControllerError:
            _record_provider_failure(
                platform,
                snapshot,
                state_file=settings.state_file,
                code="GCE_SCALE_UP_FAILED",
                detail="GCE fallback scale-up failed",
            )
            raise
    scale_in_deferral: str | None = None
    if target < current_target:
        # The overflow target ignores active leases, and the lease guard above
        # used the first inventory read. The post-renew re-read below is the
        # guard that keeps a screening GCE worker from being deleted.
        try:
            platform.fence(epoch=settings.epoch)
            instances, scale_in_deferral = _plan_gce_scale_in(
                platform,
                gce_fleet,
                target=target,
                current_target=current_target,
                claims_fenced_at_zero=(
                    starting_provider_ready
                    and any(
                        priority[0] == "hetzner"
                        for priority in (
                            provider_routing.build_provider_priority,
                            provider_routing.runtime_provider_priority,
                            provider_routing.source_review_provider_priority,
                        )
                    )
                ),
            )
            if scale_in_deferral is None and target == 0:
                # Every instance is idle, so the group may pick any of them.
                gce_fleet.resize(target)
            elif scale_in_deferral is None:
                gce_fleet.delete_instances(instances)
        except ControllerError:
            _record_provider_failure(
                platform,
                snapshot,
                state_file=settings.state_file,
                code="GCE_SCALE_DOWN_FAILED",
                detail="GCE scale-down failed",
            )
            raise
    provider_ready = provider_error_code is None
    completed = {
        **snapshot,
        # The fenced first renew already delivered this pass's events.
        "events": [],
        "provider_ready": provider_ready,
        "last_provider_error_code": (None if provider_ready else provider_error_code),
        "last_provider_error_at": None if provider_ready else provider_error_at,
    }
    if scale_in_deferral is not None:
        # A deferral is not a provider failure. Keep publishing the lower
        # target: republishing the live fleet would reopen overflow claims the
        # fenced renew withdrew, even for a closed or unknown primary, while
        # existing leases complete either way.
        completed.update(
            fallback_reason="GCE_SCALE_IN_DEFERRED",
            events=[
                {
                    "event_type": "gce_scale_in_deferred",
                    "provider": "gcp",
                    "detail": (
                        f"GCE target {current_target} -> {target} deferred: "
                        f"{scale_in_deferral}"
                    ),
                }
            ],
        )
    # Readiness describes a fully completed reconciliation pass. Persist it so
    # a failed pass cannot publish an optimistic heartbeat on the next retry.
    platform.renew(completed)
    _persist_provider_state(
        settings.state_file,
        ready=provider_ready,
        error_code=completed["last_provider_error_code"],
        error_at=completed["last_provider_error_at"],
    )
    return completed


def _settings(args: argparse.Namespace) -> Settings:
    return Settings(
        platform_url=args.platform_url,
        platform_token_file=Path(args.platform_token_file),
        environment=args.environment,
        epoch=f"{args.environment}:{uuid4()}",
        source_sha=_source_sha(),
        global_cap=args.global_cap,
        jobs_per_slot=args.jobs_per_slot,
        interval_seconds=args.interval_seconds,
        state_file=Path(args.state_file),
        gce_project=args.gce_project,
        gce_region=args.gce_region,
        gce_mig=args.gce_mig,
        gce_impersonate_service_account=args.gce_impersonate_service_account,
        lock_file=Path(args.lock_file),
        dry_run=args.dry_run,
        inventory_failure_hold_passes=args.inventory_failure_hold_passes,
    )


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-url", required=True)
    parser.add_argument("--platform-token-file", required=True)
    parser.add_argument("--environment", default="prod")
    parser.add_argument("--global-cap", type=int, default=6)
    parser.add_argument("--jobs-per-slot", type=int, default=6)
    parser.add_argument("--interval-seconds", type=int, default=30)
    parser.add_argument(
        "--inventory-failure-hold-passes", type=_non_negative_int, default=4
    )
    parser.add_argument(
        "--state-file", default="/var/lib/ditto-screener-capacity/state.json"
    )
    parser.add_argument("--gce-project", required=True)
    parser.add_argument("--gce-region", required=True)
    parser.add_argument("--gce-mig", required=True)
    parser.add_argument("--gce-impersonate-service-account")
    parser.add_argument("--lock-file", default="/run/lock/ditto-screener-capacity.lock")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    # Installed systemd units may predate the Ansible template that removed
    # these options. Accept their inert argv until those hosts are converged.
    for retired_flag in (
        "--targon-api-key-file",
        "--targon-org-slug",
        "--targon-prefix",
        "--targon-platform-url",
        "--targon-capability-file",
        "--targon-resource",
        "--targon-worker-env-file",
        "--gcp-bootstrap-service-account",
        "--gcp-bootstrap-delegate-service-account",
        "--source-review-secret-resource",
        "--targon-provisioning-timeout-seconds",
    ):
        parser.add_argument(retired_flag, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    settings = _settings(args)
    settings.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with settings.lock_file.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("capacity controller already running", file=sys.stderr)
            return 75
        while True:
            try:
                result = reconcile(settings)
                print(json.dumps(result, sort_keys=True))
            except ControllerError as error:
                print(str(error), file=sys.stderr)
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(settings.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
