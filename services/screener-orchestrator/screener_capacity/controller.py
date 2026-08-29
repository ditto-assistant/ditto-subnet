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
    def targon_first(self) -> bool:
        """True when Kaniko, runtime smoke, and L1 all start with Targon."""
        return (
            bool(self.build_provider_priority)
            and self.build_provider_priority[0] == "targon"
            and bool(self.runtime_provider_priority)
            and self.runtime_provider_priority[0] == "targon"
            and bool(self.source_review_provider_priority)
            and self.source_review_provider_priority[0] == "targon"
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
    """Choose GCE only for an explicit GCP route, outage, or queue overflow."""
    if jobs_per_slot < 1 or global_cap < 0:
        raise ValueError("capacity inputs are out of range")
    if routing.targon_first:
        return (
            min(global_cap, demand.desired),
            "TARGON_NESTED_DOCKER_WORKER_LANE_RETIRED",
        )
    if routing.gcp_first:
        return min(global_cap, demand.desired), "GCP_SCREENERS_PRIORITIZED_BY_POLICY"
    policy = routing.overflow
    if not routing.hetzner_first or not policy.enabled:
        return 0, "GCE_OVERFLOW_DISABLED"
    cap = min(global_cap, policy.max_instances)
    if cap == 0:
        return 0, "GCE_OVERFLOW_CAPPED_AT_ZERO"
    primary_ready = primary_node is not None and bool(
        primary_node.get("status") == "active" and primary_node.get("ready") is True
    )
    if not primary_ready:
        return min(cap, demand.desired), "HETZNER_PRIMARY_UNAVAILABLE"
    assert primary_node is not None
    screening_concurrency = int(primary_node.get("screening_concurrency", 0))
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
        body = _json_request(
            "GET",
            f"{self._base}/api/v1/screener/controller/provider-settings"
            f"?environment={self.environment}",
            token=self._token,
        )
        if not isinstance(body, dict):
            raise ControllerError("Platform provider settings response is invalid")
        revision = body.get("revision")
        values = body.get("settings")
        if (
            not isinstance(revision, int)
            or revision < 0
            or not isinstance(values, dict)
        ):
            raise ControllerError("Platform provider settings response is invalid")

        def priority(
            field: str,
        ) -> tuple[Literal["hetzner", "targon", "gcp"], ...]:
            raw = values.get(field)
            if (
                not isinstance(raw, list)
                or not raw
                or not all(item in {"hetzner", "targon", "gcp"} for item in raw)
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
        except (KeyError, TypeError, ValueError) as error:
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
        return result

    def drain_node(
        self, *, node_id: str, epoch: str, reason: str = "capacity scale-down"
    ) -> None:
        _json_request(
            "PUT",
            f"{self._base}/api/v1/screener/controller/nodes/{node_id}",
            token=self._token,
            payload={
                "environment": self.environment,
                "status": "draining",
                "reason": f"capacity controller {reason}",
                "controller_epoch": epoch,
            },
            allow_not_found=True,
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
        """Recover the independent scale-out watchdog after an interrupted resize."""
        if self._autoscaler_mode() != self.WATCHDOG_MODE:
            self._set_autoscaler_mode("only-scale-out")

    def resize(self, target: int) -> None:
        # Compute rejects manual resize while any autoscaler mode is active,
        # including ONLY_SCALE_OUT. Keep the emergency policy configured, pause
        # it only around the fenced mutation, and restore it even on failure.
        resize_error: ControllerError | None = None
        try:
            self._set_autoscaler_mode("off")
            self._run(
                "compute",
                "instance-groups",
                "managed",
                "resize",
                self.mig,
                "--region",
                self.region,
                "--size",
                str(target),
            )
        except ControllerError as error:
            resize_error = error
        try:
            self._set_autoscaler_mode("only-scale-out")
        except ControllerError as restore_error:
            raise ControllerError(
                "GCE autoscaler watchdog restore failed"
            ) from restore_error
        if resize_error is not None:
            raise resize_error


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


def _policy_reason(
    provider_routing: ProviderRouting, *, available: bool, targon_first: bool
) -> str:
    if not available:
        return "PROVIDER_ROUTING_UNAVAILABLE"
    if targon_first:
        return "TARGON_NESTED_DOCKER_WORKER_LANE_RETIRED"
    if (
        "targon" not in provider_routing.runtime_provider_priority
        or "targon" not in provider_routing.source_review_provider_priority
        or "targon" not in provider_routing.build_provider_priority
    ):
        return "TARGON_SCREENERS_DISABLED_BY_POLICY"
    return "GCP_SCREENERS_PRIORITIZED_BY_POLICY"


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
    provider_routing_available = True
    try:
        provider_routing = platform.provider_routing()
    except ControllerError:
        # Platform is deployed before the controller in the normal release, but
        # a rolling boundary or transient read failure must never resurrect
        # Targon against an unknown operator setting. Route through GCP until a
        # revision can be read.
        provider_routing_available = False
        provider_routing = ProviderRouting(
            revision=0,
            runtime_provider_priority=("gcp",),
            source_review_provider_priority=("gcp",),
            build_provider_priority=("gcp",),
        )
    targon_first = provider_routing.targon_first
    reason = _policy_reason(
        provider_routing,
        available=provider_routing_available,
        targon_first=targon_first,
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
    provider_success_at: str | None = None
    provider_error_code: str | None = None
    provider_error_at: str | None = None
    if not provider_routing_available:
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
    target, reason = gce_overflow_target(
        demand=demand,
        routing=provider_routing,
        primary_node=primary_node,
        jobs_per_slot=settings.jobs_per_slot,
        global_cap=settings.global_cap,
    )
    if not provider_routing_available:
        reason = "PROVIDER_ROUTING_UNAVAILABLE"
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
    events: list[dict[str, Any]] = []
    if target != current_target:
        events.append(
            {
                "event_type": "gce_target_changed",
                "provider": "gcp",
                "detail": f"GCE target {current_target} -> {target}",
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
    if target < current_target:
        # Zero is intentional.  Scale-in happens only when active leases have
        # fallen to zero because desired_slots includes every active lease.
        try:
            platform.fence(epoch=settings.epoch)
            gce_fleet.resize(target)
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
        "provider_ready": provider_ready,
        "last_provider_error_code": (None if provider_ready else provider_error_code),
        "last_provider_error_at": None if provider_ready else provider_error_at,
    }
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
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-url", required=True)
    parser.add_argument("--platform-token-file", required=True)
    parser.add_argument("--environment", default="prod")
    parser.add_argument("--global-cap", type=int, default=6)
    parser.add_argument("--jobs-per-slot", type=int, default=6)
    parser.add_argument("--interval-seconds", type=int, default=30)
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
    # Accept retired unit flags until Ansible reapplies the updated template.
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
