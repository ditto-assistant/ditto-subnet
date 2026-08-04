"""Targon-first screener capacity reconciler with a zero-idle GCE fallback.

The normal writer is fenced by the Platform lease.  Provider mutations happen
only after the snapshot heartbeat succeeds, so a stale second process cannot
resize the fleet.  Targon Rentals stay fail-closed unless an operator-provided
capability attestation says ``go``; the current live rootless-Docker probe is a
NOGO and therefore routes all residual demand to the existing GCE MIG.
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
from typing import Any, Literal
from uuid import uuid4

from screener_capacity.targon import TargonAPIError, TargonClient

Capability = Literal["go", "nogo", "unknown"]
IMAGE_REFERENCE_RE = re.compile(
    r"^[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$"
)

_SENSITIVE_ENV_MARKERS = (
    "CREDENTIAL",
    "KEY",
    "MNEMONIC",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)


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


def desired_slots(*, runnable: int, active: int, jobs_per_slot: int, cap: int) -> int:
    """Keep every active lease supplied and add bounded catch-up capacity."""
    if min(runnable, active, cap) < 0 or jobs_per_slot < 1:
        raise ValueError("capacity inputs are out of range")
    return min(cap, active + math.ceil(runnable / jobs_per_slot))


def fallback_target(
    *, demand: int, targon: ProviderCounts, capability: Capability
) -> int:
    """Return residual GCE capacity; NOGO/unknown routes all demand to GCE."""
    if capability != "go":
        return demand
    return max(0, demand - targon.supplied)


def _read_secret_file(path: Path) -> str:
    try:
        value = path.read_text().strip()
    except OSError as error:
        raise ControllerError(f"credential file is unavailable: {path.name}") from error
    if len(value) < 32:
        raise ControllerError(f"credential file is invalid: {path.name}")
    return value


def _validate_public_worker_env(values: dict[str, str]) -> None:
    """Reject durable credentials from provider-visible workload metadata."""
    sensitive = sorted(
        key
        for key in values
        if any(marker in key.upper() for marker in _SENSITIVE_ENV_MARKERS)
    )
    if sensitive:
        raise ControllerError(
            "Targon worker environment contains prohibited credential-like keys: "
            + ", ".join(sensitive)
        )


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

    def latest_screener_image(self) -> str | None:
        body = _json_request(
            "GET",
            f"{self._base}/api/v1/screener/controller/trusted-image-builds/latest"
            f"?environment={self.environment}",
            token=self._token,
            allow_not_found=True,
        )
        if body is None:
            return None
        if not isinstance(body, dict) or body.get("status") != "succeeded":
            raise ControllerError("Platform released image response is invalid")
        destination = body.get("destination")
        digest = body.get("image_digest")
        if not isinstance(destination, str) or not isinstance(digest, str):
            raise ControllerError("Platform released image identity is incomplete")
        repository = destination.rsplit(":", 1)[0]
        reference = f"{repository}@{digest}"
        if IMAGE_REFERENCE_RE.fullmatch(reference) is None:
            raise ControllerError("Platform released image identity is invalid")
        return reference

    def bootstrap_grant(
        self,
        *,
        node_id: str,
        resource_id: str,
        epoch: str,
        image_reference: str,
    ) -> str:
        body = _json_request(
            "POST",
            f"{self._base}/api/v1/screener/controller/bootstrap-grants",
            token=self._token,
            payload={
                "environment": self.environment,
                "node_id": node_id,
                "provider": "targon",
                "provider_resource_id": resource_id,
                "controller_epoch": epoch,
                "image_reference": image_reference,
            },
        )
        token = body.get("registration_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or len(token) < 43:
            raise ControllerError("Platform bootstrap response is invalid")
        return token

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

    def resize(self, target: int) -> None:
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


def _capability(path: Path | None) -> tuple[Capability, str | None]:
    if path is None or not path.exists():
        return "unknown", "TARGON_CAPABILITY_ATTESTATION_MISSING"
    try:
        body = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return "nogo", "TARGON_CAPABILITY_ATTESTATION_INVALID"
    result = str(body.get("result", "")).casefold()
    if result not in {"go", "nogo"}:
        return "nogo", "TARGON_CAPABILITY_ATTESTATION_INVALID"
    # Attestations deliberately expire.  A provider/runtime change must be
    # re-probed rather than silently inheriting an old GO.
    expires_at = body.get("expires_at")
    try:
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return "nogo", "TARGON_CAPABILITY_ATTESTATION_INVALID"
    if expiry <= datetime.now(UTC):
        return "nogo", "TARGON_CAPABILITY_ATTESTATION_EXPIRED"
    reason_code = body.get("reason_code")
    capability: Capability = "go" if result == "go" else "nogo"
    return capability, reason_code if isinstance(reason_code, str) else None


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
    stuck_uids: set[str],
    *,
    desired_image: str | None = None,
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
        image_outdated = desired_image is not None and (
            node.get("image_reference") != desired_image
        )
        if (
            status == "running"
            and isinstance(ready, int)
            and ready > 0
            and node.get("ready") is True
            and not image_outdated
        ):
            healthy += 1
        elif (
            image_outdated
            or uid in stuck_uids
            or status
            in {
                "suspending",
                "suspended",
                "deleting",
            }
            or node.get("status") in {"draining", "quarantined", "revoked"}
        ):
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


def _pending_state(
    path: Path,
    rows: list[dict[str, Any]],
    node_states: dict[str, dict[str, Any]],
    *,
    timeout_seconds: int,
) -> set[str]:
    """Persist first-seen times so stuck provisioning survives restarts."""
    loaded = _load_state(path)
    prior = loaded.get("pending_since", {})
    if not isinstance(prior, dict):
        prior = {}
    now = time.time()
    pending_since: dict[str, float] = {}
    for row in rows:
        uid = str(row.get("uid", ""))
        if not uid or node_states.get(uid, {}).get("ready") is True:
            continue
        value = prior.get(uid, now)
        pending_since[uid] = (
            float(value)
            if isinstance(value, (int, float)) and 0 <= value <= now
            else now
        )
    loaded["pending_since"] = pending_since
    _write_state(path, loaded)
    return {
        uid
        for uid, started in pending_since.items()
        if now - started >= timeout_seconds
    }


@dataclass(frozen=True)
class Settings:
    platform_url: str
    targon_platform_url: str
    platform_token_file: Path
    environment: str
    epoch: str
    source_sha: str
    global_cap: int
    jobs_per_slot: int
    interval_seconds: int
    targon_capability_file: Path | None
    targon_api_key_file: Path | None
    targon_prefix: str
    targon_resource: str
    targon_worker_env_file: Path | None
    gcp_bootstrap_service_account: str | None
    gcp_bootstrap_delegate_service_account: str | None
    source_review_secret_resource: str | None
    targon_provisioning_timeout_seconds: int
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
    demand: Demand,
    capability: Capability,
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
        "runnable_backlog": demand.runnable,
        "active_leases": demand.active,
        "desired_slots": demand.desired,
        "global_cap": settings.global_cap,
        "provider_ready": provider_ready,
        "targon_capability": capability,
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
    capability, reason = _capability(settings.targon_capability_file)
    desired_targon_image: str | None = None
    if capability == "go":
        desired_targon_image = platform.latest_screener_image()
        if desired_targon_image is None:
            capability = "nogo"
            reason = "TARGON_WORKER_IMAGE_UNPUBLISHED"
    targon_counts = ProviderCounts()
    targon_rows: list[dict[str, Any]] = []
    targon_client: TargonClient | None = None
    targon_available = 0
    node_states: dict[str, dict[str, Any]] = {}
    stuck_uids: set[str] = set()
    provider_success_at: str | None = None
    provider_error_code: str | None = None
    provider_error_at: str | None = None
    if settings.targon_api_key_file is not None:
        key = _read_secret_file(settings.targon_api_key_file)
        targon_client = TargonClient(api_key=key)
        try:
            targon_rows = _owned_targon_workloads(targon_client, settings.targon_prefix)
            try:
                node_states = platform.node_states()
            except ControllerError:
                capability = "nogo"
                reason = "TARGON_HEARTBEAT_STATE_UNAVAILABLE"
                provider_error_code = reason
                provider_error_at = datetime.now(UTC).isoformat()
            stuck_uids = _pending_state(
                settings.state_file,
                targon_rows,
                node_states,
                timeout_seconds=settings.targon_provisioning_timeout_seconds,
            )
            targon_counts = _targon_counts(
                targon_rows,
                node_states,
                stuck_uids,
                desired_image=(desired_targon_image if capability == "go" else None),
            )
            targon_available = sum(
                max(0, int(row.get("available", 0)))
                for row in targon_client.inventory()
                if str(row.get("name", "")) == settings.targon_resource
            )
            provider_success_at = datetime.now(UTC).isoformat()
        except TargonAPIError:
            capability = "nogo"
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

    outdated_targon_uids = {
        str(row.get("uid", ""))
        for row in targon_rows
        if capability == "go"
        and str(row.get("uid", ""))
        and node_states.get(str(row.get("uid", "")), {}).get("image_reference")
        != desired_targon_image
    }

    worker_env: dict[str, str] = {}
    if capability == "go":
        if targon_client is None or desired_targon_image is None:
            capability = "nogo"
            reason = "TARGON_WORKER_CONFIG_MISSING"
        elif (
            settings.gcp_bootstrap_service_account is None
            or settings.source_review_secret_resource is None
        ):
            capability = "nogo"
            reason = "TARGON_SECRET_BOOTSTRAP_MISSING"
        elif settings.targon_worker_env_file is not None:
            try:
                loaded_env = json.loads(settings.targon_worker_env_file.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise ControllerError(
                    "Targon worker environment file is invalid"
                ) from error
            if not isinstance(loaded_env, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in loaded_env.items()
            ):
                raise ControllerError("Targon worker environment must be string pairs")
            _validate_public_worker_env(loaded_env)
            worker_env = loaded_env

    # Capacity planned for this pass counts as pending before computing the GCE
    # residual.  That prevents both providers scaling to the full demand.
    planned_targon = targon_counts.supplied
    if capability == "go":
        occupied_names = {str(row.get("name", "")) for row in targon_rows}
        available_slot_count = sum(
            1
            for slot in range(1, settings.global_cap + 1)
            if f"{settings.targon_prefix}slot-{slot:02d}"[:32] not in occupied_names
        )
        new_capacity_room = max(
            0,
            settings.global_cap - current_target - targon_counts.supplied,
        )
        planned_targon = targon_counts.supplied + min(
            max(0, demand.desired - targon_counts.supplied),
            max(0, targon_available),
            new_capacity_room,
            available_slot_count,
        )
    planned_counts = ProviderCounts(
        healthy=targon_counts.healthy,
        pending=max(0, planned_targon - targon_counts.healthy),
        draining=targon_counts.draining,
    )

    target = fallback_target(
        demand=demand.desired, targon=planned_counts, capability=capability
    )
    if target < current_target and demand.active > 0:
        # Never remove GCE capacity while any provider still owns a live lease.
        # Recovered Targon becomes the next-wave primary only after drain.
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
    if capability != "go" and targon_rows:
        events.append(
            {
                "event_type": "targon_fail_closed",
                "provider": "targon",
                "detail": f"Draining {len(targon_rows)} Targon workload(s): {reason}",
            }
        )
    if stuck_uids:
        events.append(
            {
                "event_type": "targon_provisioning_stuck",
                "provider": "targon",
                "detail": f"{len(stuck_uids)} workload(s) exceeded provisioning grace",
            }
        )
    newly_outdated_targon_uids = {
        uid
        for uid in outdated_targon_uids
        if node_states.get(uid, {}).get("status")
        not in {"draining", "quarantined", "revoked"}
    }
    if newly_outdated_targon_uids:
        events.append(
            {
                "event_type": "targon_image_rollout",
                "provider": "targon",
                "detail": (
                    f"{len(newly_outdated_targon_uids)} workload(s) draining for "
                    f"released source {desired_targon_image}"
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
        demand=demand,
        capability=capability,
        reason=reason,
        targon=planned_counts,
        targon_available=targon_available,
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
    deleted_targon_uids: set[str] = set()
    if target > current_target:
        # Bring fallback capacity up before touching Targon. A teardown failure
        # must never prevent the independent GCE safety path from scaling out.
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
    if capability != "go" and targon_client is not None:
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
            try:
                state = row.get("state") or {}
                if str(state.get("status", "")).casefold() not in {
                    "suspended",
                    "registered",
                    "error",
                }:
                    platform.fence(epoch=settings.epoch)
                    targon_client.suspend(uid)
                platform.fence(epoch=settings.epoch)
                targon_client.delete(uid)
                deleted_targon_uids.add(uid)
            except (TargonAPIError, ControllerError) as error:
                _record_provider_failure(
                    platform,
                    snapshot,
                    state_file=settings.state_file,
                    code="TARGON_TEARDOWN_FAILED",
                    detail="Targon fail-closed teardown failed",
                )
                raise ControllerError("Targon fail-closed teardown failed") from error
        for node in node_states.values():
            node_id = str(node.get("node_id", ""))
            if (
                node.get("provider") == "targon"
                and node.get("status") == "active"
                and node_id
                and node_id not in drained_node_ids
            ):
                platform.drain_node(node_id=node_id, epoch=settings.epoch)
    elif capability == "go" and targon_client is not None:
        for row in targon_rows:
            uid = str(row.get("uid", ""))
            name = str(row.get("name", ""))
            if uid not in outdated_targon_uids or not name:
                continue
            node = node_states.get(uid, {})
            node_id = str(node.get("node_id", name))
            already_retiring = node.get("status") in {
                "draining",
                "quarantined",
                "revoked",
            }
            if not already_retiring:
                platform.drain_node(
                    node_id=node_id,
                    epoch=settings.epoch,
                    reason="release-image replacement",
                )
                # Give the individual worker a reconciliation pass to stop
                # accepting work. Other nodes may stay busy indefinitely.
                if node:
                    continue
            if _node_has_active_lease(node):
                continue
            try:
                state = row.get("state") or {}
                if str(state.get("status", "")).casefold() not in {
                    "suspended",
                    "registered",
                    "error",
                }:
                    platform.fence(epoch=settings.epoch)
                    targon_client.suspend(uid)
                platform.fence(epoch=settings.epoch)
                targon_client.delete(uid)
                deleted_targon_uids.add(uid)
            except (TargonAPIError, ControllerError) as error:
                _record_provider_failure(
                    platform,
                    snapshot,
                    state_file=settings.state_file,
                    code="TARGON_IMAGE_ROLLOUT_FAILED",
                    detail="Targon released-image teardown failed",
                )
                raise ControllerError("Targon image rollout teardown failed") from error
        for row in targon_rows:
            uid = str(row.get("uid", ""))
            name = str(row.get("name", ""))
            if uid not in stuck_uids or not name:
                continue
            node_id = str(node_states.get(uid, {}).get("node_id", name))
            node = node_states.get(uid, {})
            already_retiring = node.get("status") in {
                "draining",
                "quarantined",
                "revoked",
            }
            if not already_retiring:
                platform.drain_node(node_id=node_id, epoch=settings.epoch)
                if node:
                    continue
            if _node_has_active_lease(node):
                continue
            try:
                state = row.get("state") or {}
                if str(state.get("status", "")).casefold() not in {
                    "suspended",
                    "registered",
                    "error",
                }:
                    platform.fence(epoch=settings.epoch)
                    targon_client.suspend(uid)
                platform.fence(epoch=settings.epoch)
                targon_client.delete(uid)
                deleted_targon_uids.add(uid)
            except (TargonAPIError, ControllerError) as error:
                _record_provider_failure(
                    platform,
                    snapshot,
                    state_file=settings.state_file,
                    code="TARGON_STUCK_TEARDOWN_FAILED",
                    detail="Targon stuck-workload teardown failed",
                )
                raise ControllerError(
                    "Targon stuck-workload teardown failed"
                ) from error
        for row in targon_rows:
            name = str(row.get("name", ""))
            uid = str(row.get("uid", ""))
            node = node_states.get(uid, {})
            status = node.get("status")
            if (
                not name
                or not uid
                or uid in deleted_targon_uids
                or status not in {"draining", "quarantined", "revoked"}
                or _node_has_active_lease(node)
            ):
                continue
            try:
                state = row.get("state") or {}
                if str(state.get("status", "")).casefold() not in {
                    "suspended",
                    "registered",
                    "error",
                }:
                    platform.fence(epoch=settings.epoch)
                    targon_client.suspend(uid)
                platform.fence(epoch=settings.epoch)
                targon_client.delete(uid)
                deleted_targon_uids.add(uid)
            except (TargonAPIError, ControllerError) as error:
                _record_provider_failure(
                    platform,
                    snapshot,
                    state_file=settings.state_file,
                    code="TARGON_RETIRED_TEARDOWN_FAILED",
                    detail="Targon retired-node teardown failed",
                )
                raise ControllerError("Targon retired-node teardown failed") from error
        token_minter = GCPBootstrapTokenMinter(
            target=str(settings.gcp_bootstrap_service_account),
            delegate=settings.gcp_bootstrap_delegate_service_account,
        )
        # Draining outdated nodes are not supply. Provision their replacements
        # even while unrelated nodes own leases; deletion above is lease-scoped.
        difference = planned_targon - targon_counts.supplied
        if difference > 0:
            occupied_names = {str(row.get("name", "")) for row in targon_rows}
            available_names = [
                f"{settings.targon_prefix}slot-{slot:02d}"[:32]
                for slot in range(1, settings.global_cap + 1)
                if f"{settings.targon_prefix}slot-{slot:02d}"[:32] not in occupied_names
            ]
            for name in available_names[:difference]:
                created_uid: str | None = None
                try:
                    platform.fence(epoch=settings.epoch)
                    created = targon_client.create_rental(
                        name=name,
                        image=str(desired_targon_image),
                        resource_name=settings.targon_resource,
                    )
                    created_uid = str(created["uid"])
                    node_id = f"{name}-{created_uid[-8:]}"
                    registration_token = platform.bootstrap_grant(
                        node_id=node_id,
                        resource_id=created_uid,
                        epoch=settings.epoch,
                        image_reference=str(desired_targon_image),
                    )
                    bootstrap_access_token = token_minter.mint()
                    env = {
                        **worker_env,
                        "SCREENER_NODE_ENVIRONMENT": settings.environment,
                        "SCREENER_NODE_ID": node_id,
                        "SCREENER_NODE_PROVIDER": "targon",
                        "SCREENER_NODE_PROVIDER_RESOURCE_ID": created_uid,
                        "SCREENER_NODE_CREDENTIAL_FILE": "/tmp/ditto-node.json",
                        "SCREENER_REGISTRATION_TOKEN": registration_token,
                        "SCREENER_PLATFORM_API_URL": settings.targon_platform_url,
                        "SCREENER_GCP_BOOTSTRAP_ACCESS_TOKEN": bootstrap_access_token,
                        "SCREENER_SOURCE_REVIEW_SECRET_RESOURCE": str(
                            settings.source_review_secret_resource
                        ),
                    }
                    platform.fence(epoch=settings.epoch)
                    targon_client.update(
                        created_uid,
                        envs=[
                            {"name": key, "value": value}
                            for key, value in sorted(env.items())
                        ],
                    )
                    platform.fence(epoch=settings.epoch)
                    targon_client.deploy(created_uid)
                except (TargonAPIError, ControllerError, KeyError):
                    if created_uid is not None:
                        with contextlib.suppress(TargonAPIError, ControllerError):
                            platform.fence(epoch=settings.epoch)
                            targon_client.delete(created_uid)
                    # Fast failover: a Targon create/deploy error must not leave
                    # the queue waiting for the next normal-controller pass.
                    emergency_target = max(
                        current_target,
                        demand.active,
                        demand.desired - targon_counts.supplied,
                    )
                    try:
                        if current_target != emergency_target:
                            platform.fence(epoch=settings.epoch)
                            gce_fleet.resize(emergency_target)
                    except ControllerError:
                        _record_provider_failure(
                            platform,
                            snapshot,
                            state_file=settings.state_file,
                            code="GCE_EMERGENCY_SCALE_UP_FAILED",
                            detail=(
                                "GCE emergency lease floor failed after Targon error"
                            ),
                        )
                        raise
                    _record_provider_failure(
                        platform,
                        snapshot,
                        state_file=settings.state_file,
                        code="TARGON_SCALE_UP_FAILED",
                        detail="Targon scale-up failed; GCE emergency floor requested",
                    )
                    raise ControllerError("Targon scale-up failed") from None
        excess = max(0, targon_counts.supplied - demand.desired)
        if excess > 0:
            for row in reversed(targon_rows):
                if excess == 0:
                    break
                name = str(row.get("name", ""))
                uid = str(row.get("uid", ""))
                if not name or not uid:
                    continue
                if uid in deleted_targon_uids:
                    continue
                node = node_states.get(uid, {})
                if _node_has_active_lease(node):
                    continue
                node_id = str(node.get("node_id", name))
                if node.get("status") not in {
                    "draining",
                    "quarantined",
                    "revoked",
                }:
                    platform.drain_node(node_id=node_id, epoch=settings.epoch)
                    excess -= 1
                    if node:
                        continue
                try:
                    state = row.get("state") or {}
                    if str(state.get("status", "")).casefold() not in {
                        "suspended",
                        "registered",
                        "error",
                    }:
                        platform.fence(epoch=settings.epoch)
                        targon_client.suspend(uid)
                    platform.fence(epoch=settings.epoch)
                    targon_client.delete(uid)
                    deleted_targon_uids.add(uid)
                except (TargonAPIError, ControllerError) as error:
                    _record_provider_failure(
                        platform,
                        snapshot,
                        state_file=settings.state_file,
                        code="TARGON_SCALE_DOWN_FAILED",
                        detail="Targon scale-down failed",
                    )
                    raise ControllerError("Targon scale-down failed") from error
                excess -= 1
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
        targon_platform_url=args.targon_platform_url,
        platform_token_file=Path(args.platform_token_file),
        environment=args.environment,
        epoch=f"{args.environment}:{uuid4()}",
        source_sha=_source_sha(),
        global_cap=args.global_cap,
        jobs_per_slot=args.jobs_per_slot,
        interval_seconds=args.interval_seconds,
        targon_capability_file=(
            Path(args.targon_capability_file) if args.targon_capability_file else None
        ),
        targon_api_key_file=(
            Path(args.targon_api_key_file) if args.targon_api_key_file else None
        ),
        targon_prefix=args.targon_prefix,
        targon_resource=args.targon_resource,
        targon_worker_env_file=(
            Path(args.targon_worker_env_file) if args.targon_worker_env_file else None
        ),
        gcp_bootstrap_service_account=args.gcp_bootstrap_service_account,
        gcp_bootstrap_delegate_service_account=(
            args.gcp_bootstrap_delegate_service_account
        ),
        source_review_secret_resource=args.source_review_secret_resource,
        targon_provisioning_timeout_seconds=(args.targon_provisioning_timeout_seconds),
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
    parser.add_argument("--targon-platform-url", required=True)
    parser.add_argument("--platform-token-file", required=True)
    parser.add_argument("--environment", default="prod")
    parser.add_argument("--global-cap", type=int, default=6)
    parser.add_argument("--jobs-per-slot", type=int, default=6)
    parser.add_argument("--interval-seconds", type=int, default=30)
    parser.add_argument("--targon-capability-file")
    parser.add_argument("--targon-api-key-file")
    parser.add_argument("--targon-prefix", default="ditto-screener-prod-")
    parser.add_argument("--targon-resource", default="cpu-medium")
    parser.add_argument("--targon-worker-env-file")
    parser.add_argument("--gcp-bootstrap-service-account")
    parser.add_argument("--gcp-bootstrap-delegate-service-account")
    parser.add_argument("--source-review-secret-resource")
    parser.add_argument("--targon-provisioning-timeout-seconds", type=int, default=600)
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
