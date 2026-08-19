"""GCE residual screener capacity reconciler.

Nested-Docker Targon screener slots are retired. Platform owns Kaniko, runtime
smoke, and L1 one-shot Targon rentals. This process still fences GCE MIG
mutations: Targon-first decomposed lanes keep the fleet at zero, and a GCE-only
cutover scales the MIG to residual demand. Leftover ``ditto-screener-*-slot-*``
rentals are drained and deleted; they are never created.
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

from screener_capacity.targon import TargonAPIError, TargonClient


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
class ProviderRouting:
    revision: int
    runtime_provider_priority: tuple[Literal["targon", "gcp"], ...]
    source_review_provider_priority: tuple[Literal["targon", "gcp"], ...]
    build_provider_priority: tuple[Literal["targon", "gcp"], ...]

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


def desired_slots(*, runnable: int, active: int, jobs_per_slot: int, cap: int) -> int:
    """Keep every active lease supplied and add bounded catch-up capacity."""
    if min(runnable, active, cap) < 0 or jobs_per_slot < 1:
        raise ValueError("capacity inputs are out of range")
    return min(cap, active + math.ceil(runnable / jobs_per_slot))


def gce_residual(*, demand: int, targon_first: bool) -> int:
    """Return GCE MIG capacity for screening workers.

    Nested-Docker Targon slots are retired, so there is no Targon worker
    residual. Targon-first decomposed lanes keep the MIG at zero; any GCE-only
    or mixed revision still uses the fleet.
    """
    if targon_first:
        return 0
    return demand


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

        def priority(field: str) -> tuple[Literal["targon", "gcp"], ...]:
            raw = values.get(field)
            if (
                not isinstance(raw, list)
                or not raw
                or not all(item in {"targon", "gcp"} for item in raw)
                or len(raw) != len(set(raw))
                or "gcp" not in raw
            ):
                raise ControllerError("Platform provider priority is invalid")
            return cast(tuple[Literal["targon", "gcp"], ...], tuple(raw))

        return ProviderRouting(
            revision=revision,
            runtime_provider_priority=priority("runtime_provider_priority"),
            source_review_provider_priority=priority("source_review_provider_priority"),
            build_provider_priority=priority("build_provider_priority"),
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
        return {
            str(row["provider_resource_id"]): row
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("node_id"), str)
            and isinstance(row.get("provider_resource_id"), str)
        }

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


def _owned_targon_workloads(client: TargonClient, prefix: str) -> list[dict[str, Any]]:
    return [
        row
        for row in client.list_workloads()
        if str(row.get("name", "")).startswith(prefix)
        and str((row.get("state") or {}).get("status", "")).casefold() != "deleted"
    ]


def _targon_counts(
    rows: list[dict[str, Any]],
    node_states: dict[str, dict[str, Any]],
) -> ProviderCounts:
    healthy = pending = draining = 0
    for row in rows:
        state = row.get("state")
        status = (
            str(state.get("status", "")).casefold() if isinstance(state, dict) else ""
        )
        ready = state.get("ready_replicas", 0) if isinstance(state, dict) else 0
        uid = str(row.get("uid", ""))
        node = node_states.get(uid, {})
        if (
            status == "running"
            and isinstance(ready, int)
            and ready > 0
            and node.get("ready") is True
            and node.get("status") == "active"
        ):
            healthy += 1
        elif status in {"suspending", "suspended", "deleting"} or node.get(
            "status"
        ) in {
            "draining",
            "quarantined",
            "revoked",
        }:
            draining += 1
        else:
            pending += 1
    return ProviderCounts(healthy=healthy, pending=pending, draining=draining)


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
    targon_api_key_file: Path | None
    targon_org_slug: str
    targon_prefix: str
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
    targon: ProviderCounts,
    targon_available: int,
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
        "targon_available": targon_available,
        "targon_healthy": targon.healthy,
        "targon_pending": targon.pending,
        "targon_draining": targon.draining,
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


def _node_has_active_lease(node: dict[str, Any]) -> bool:
    return node.get("active_lease") is True


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


def _delete_targon_workload(
    *,
    platform: PlatformControl,
    client: TargonClient,
    uid: str,
    row: dict[str, Any],
    snapshot: dict[str, Any],
    settings: Settings,
) -> None:
    try:
        state = row.get("state") or {}
        if str(state.get("status", "")).casefold() not in {
            "suspended",
            "registered",
            "error",
        }:
            platform.fence(epoch=settings.epoch)
            client.suspend(uid)
        platform.fence(epoch=settings.epoch)
        client.delete(uid)
    except (TargonAPIError, ControllerError) as error:
        _record_provider_failure(
            platform,
            snapshot,
            state_file=settings.state_file,
            code="TARGON_TEARDOWN_FAILED",
            detail="Targon leftover nested-Docker teardown failed",
        )
        raise ControllerError(
            "Targon leftover nested-Docker teardown failed"
        ) from error


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
    targon_counts = ProviderCounts()
    targon_rows: list[dict[str, Any]] = []
    targon_client: TargonClient | None = None
    node_states: dict[str, dict[str, Any]] = {}
    provider_success_at: str | None = None
    provider_error_code: str | None = None
    provider_error_at: str | None = None
    if not provider_routing_available:
        provider_error_code = "PROVIDER_ROUTING_UNAVAILABLE"
        provider_error_at = datetime.now(UTC).isoformat()
    if settings.targon_api_key_file is not None:
        key = _read_secret_file(settings.targon_api_key_file)
        targon_client = TargonClient(api_key=key, org_slug=settings.targon_org_slug)
        try:
            targon_rows = _owned_targon_workloads(targon_client, settings.targon_prefix)
            try:
                node_states = platform.node_states()
            except ControllerError:
                node_states = {}
                provider_error_code = "TARGON_HEARTBEAT_STATE_UNAVAILABLE"
                provider_error_at = datetime.now(UTC).isoformat()
            targon_counts = _targon_counts(targon_rows, node_states)
            provider_success_at = datetime.now(UTC).isoformat()
        except TargonAPIError:
            reason = "TARGON_API_UNAVAILABLE"
            provider_error_code = reason
            provider_error_at = datetime.now(UTC).isoformat()
            try:
                node_states = platform.node_states()
            except ControllerError:
                node_states = {}

    gce_fleet = GCEFleet(
        project=settings.gce_project,
        region=settings.gce_region,
        mig=settings.gce_mig,
        impersonate_service_account=settings.gce_impersonate_service_account,
    )
    current_target = gce_fleet.target()
    gce_counts = gce_fleet.counts()
    provider_success_at = datetime.now(UTC).isoformat()

    target = gce_residual(demand=demand.desired, targon_first=targon_first)
    if target < current_target and demand.active > 0:
        # Never remove GCE capacity while any provider still owns a live lease.
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
    if targon_rows:
        events.append(
            {
                "event_type": "targon_fail_closed",
                "provider": "targon",
                "detail": (
                    f"Draining {len(targon_rows)} leftover nested-Docker "
                    "Targon worker(s)"
                ),
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
        targon=targon_counts,
        targon_available=targon_counts.supplied,
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
        # Bring fallback capacity up before touching leftover Targon slots. A
        # teardown failure must never prevent the GCE safety path from scaling
        # out.
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
    if targon_client is not None:
        drained_node_ids: set[str] = set()
        for row in targon_rows:
            name = str(row.get("name", ""))
            uid = str(row.get("uid", ""))
            if not name or not uid:
                continue
            node = node_states.get(uid, {})
            node_id = str(node.get("node_id", name))
            already_retiring = node.get("status") in {
                "draining",
                "quarantined",
                "revoked",
            }
            if not already_retiring:
                platform.drain_node(node_id=node_id, epoch=settings.epoch)
                # A registered node gets one full reconciliation pass to
                # observe drain state and finish its own lease.
                if node:
                    drained_node_ids.add(node_id)
                    continue
            drained_node_ids.add(node_id)
            if _node_has_active_lease(node):
                continue
            _delete_targon_workload(
                platform=platform,
                client=targon_client,
                uid=uid,
                row=row,
                snapshot=snapshot,
                settings=settings,
            )
        for node in node_states.values():
            node_id = str(node.get("node_id", ""))
            if (
                node.get("provider") == "targon"
                and node.get("status") == "active"
                and node_id
                and node_id not in drained_node_ids
            ):
                platform.drain_node(node_id=node_id, epoch=settings.epoch)
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
        targon_api_key_file=(
            Path(args.targon_api_key_file) if args.targon_api_key_file else None
        ),
        targon_org_slug=args.targon_org_slug,
        targon_prefix=args.targon_prefix,
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
    parser.add_argument("--targon-api-key-file")
    parser.add_argument("--targon-org-slug", required=True)
    parser.add_argument("--targon-prefix", default="ditto-screener-prod-")
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
