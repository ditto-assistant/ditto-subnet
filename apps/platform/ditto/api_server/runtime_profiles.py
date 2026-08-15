"""Private, bounded capture of loopback-only Go relay profiles."""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import httpx

from ditto.api_models.inference_observability import (
    RuntimeProfileArtifact,
    RuntimeProfileTarget,
    RuntimeProfileType,
)

PROFILE_TTL = timedelta(minutes=15)
MAX_PROFILE_BYTES: Final = 4 * 1024 * 1024
PROFILE_ROOT = Path("/tmp/ditto-runtime-profiles")
TARGET_PORTS: Final[dict[RuntimeProfileTarget, tuple[int, int]]] = {
    "platform-relay-1": (8010, 11010),
    "platform-relay-2": (8011, 11011),
}
PROFILE_PATHS: Final = {
    "cpu": "profile",
    "heap": "heap",
    "allocs": "allocs",
    "goroutine": "goroutine",
}


class RuntimeProfileError(RuntimeError):
    """A safe operator-facing capture or artifact error."""


class RuntimeProfileBusyError(RuntimeProfileError):
    """The selected process already has a profile capture in flight."""


class RuntimeProfileNotFoundError(RuntimeProfileError):
    """The profile id is unknown or expired."""


class RuntimeProfileStore:
    def __init__(
        self,
        root: Path = PROFILE_ROOT,
        *,
        target_ports: Mapping[RuntimeProfileTarget, tuple[int, int]] = TARGET_PORTS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.root = root
        self.target_ports = target_ports
        self.transport = transport

    def _ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def _metadata_path(self, profile_id: UUID) -> Path:
        return self.root / f"{profile_id}.json"

    def _artifact_path(self, profile_id: UUID) -> Path:
        return self.root / f"{profile_id}.pb.gz"

    def _cleanup(self, now: datetime) -> None:
        self._ensure_root()
        for metadata_path in self.root.glob("*.json"):
            try:
                artifact = RuntimeProfileArtifact.model_validate_json(
                    metadata_path.read_text()
                )
            except (OSError, ValueError):
                continue
            if artifact.expires_at > now:
                continue
            self._artifact_path(artifact.profile_id).unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)

    def get(self, profile_id: UUID) -> RuntimeProfileArtifact:
        now = datetime.now(UTC)
        self._cleanup(now)
        try:
            artifact = RuntimeProfileArtifact.model_validate_json(
                self._metadata_path(profile_id).read_text()
            )
        except (OSError, ValueError) as error:
            raise RuntimeProfileNotFoundError(
                "runtime profile not found or expired"
            ) from error
        if artifact.expires_at <= now or not self._artifact_path(profile_id).is_file():
            raise RuntimeProfileNotFoundError("runtime profile not found or expired")
        return artifact

    def download(self, profile_id: UUID) -> tuple[RuntimeProfileArtifact, Path]:
        artifact = self.get(profile_id)
        return artifact, self._artifact_path(profile_id)

    async def capture(
        self,
        *,
        target: RuntimeProfileTarget,
        profile_type: RuntimeProfileType,
        seconds: int | None,
        actor: str,
        reason: str,
    ) -> RuntimeProfileArtifact:
        self._ensure_root()
        main_port, pprof_port = self.target_ports[target]
        lock_path = self.root / f"{target}.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeProfileBusyError(
                    f"{target} already has a runtime profile capture in progress"
                ) from error
            return await self._capture_locked(
                target=target,
                profile_type=profile_type,
                seconds=seconds,
                actor=actor,
                reason=reason,
                main_port=main_port,
                pprof_port=pprof_port,
            )
        finally:
            os.close(lock_fd)

    async def _capture_locked(
        self,
        *,
        target: RuntimeProfileTarget,
        profile_type: RuntimeProfileType,
        seconds: int | None,
        actor: str,
        reason: str,
        main_port: int,
        pprof_port: int,
    ) -> RuntimeProfileArtifact:
        timeout_seconds = (seconds or 0) + 10
        async with httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(max(10, timeout_seconds)),
            transport=self.transport,
        ) as client:
            try:
                health_response = await client.get(
                    f"http://127.0.0.1:{main_port}/health"
                )
                health_response.raise_for_status()
                health = health_response.json()
                params = {"seconds": seconds} if profile_type == "cpu" else None
                async with client.stream(
                    "GET",
                    f"http://127.0.0.1:{pprof_port}/debug/pprof/{PROFILE_PATHS[profile_type]}",
                    params=params,
                ) as response:
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    byte_size = 0
                    async for chunk in response.aiter_bytes():
                        byte_size += len(chunk)
                        if byte_size > MAX_PROFILE_BYTES:
                            raise RuntimeProfileError(
                                "runtime profile exceeded the "
                                f"{MAX_PROFILE_BYTES}-byte safety limit"
                            )
                        chunks.append(chunk)
            except RuntimeProfileError:
                raise
            except (httpx.HTTPError, ValueError, KeyError) as error:
                raise RuntimeProfileError(
                    f"could not capture {profile_type} from {target} loopback"
                ) from error

        source_revision = str(health.get("commit", ""))
        checked_out_revision = str(health.get("checked_out_commit", ""))
        if len(source_revision) != 40 or len(checked_out_revision) != 40:
            raise RuntimeProfileError(
                "relay health did not report exact source revisions"
            )

        payload = b"".join(chunks)
        if not payload:
            raise RuntimeProfileError(
                f"{target} returned an empty {profile_type} runtime profile"
            )
        profile_id = uuid4()
        created_at = datetime.now(UTC)
        filename = (
            f"{target}-{profile_type}-{created_at.strftime('%Y%m%dT%H%M%SZ')}.pb.gz"
        )
        artifact = RuntimeProfileArtifact(
            profile_id=profile_id,
            target=target,
            profile_type=profile_type,
            seconds=seconds,
            source_revision=source_revision,
            checked_out_revision=checked_out_revision,
            revision_drift=bool(health.get("commit_drift", False)),
            actor=actor,
            reason=reason,
            created_at=created_at,
            expires_at=created_at + PROFILE_TTL,
            byte_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            filename=filename,
            download_path=f"/api/v1/admin/runtime-profiles/{profile_id}/download",
        )
        artifact_path = self._artifact_path(profile_id)
        metadata_path = self._metadata_path(profile_id)
        artifact_path.write_bytes(payload)
        os.chmod(artifact_path, 0o600)
        metadata_path.write_text(artifact.model_dump_json())
        os.chmod(metadata_path, 0o600)
        self._cleanup(created_at)
        return artifact
