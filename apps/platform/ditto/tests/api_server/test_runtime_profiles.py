from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from ditto.api_models.inference_observability import RuntimeProfileCaptureRequest
from ditto.api_server.runtime_profiles import (
    MAX_PROFILE_BYTES,
    RuntimeProfileError,
    RuntimeProfileNotFoundError,
    RuntimeProfileStore,
)

_REVISION = "a" * 40


def _transport(profile: bytes = b"pprof-profile") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "commit": _REVISION,
                    "checked_out_commit": _REVISION,
                    "commit_drift": False,
                },
            )
        if request.url.path == "/debug/pprof/profile":
            assert request.url.params["seconds"] == "15"
            return httpx.Response(200, content=profile)
        if request.url.path == "/debug/pprof/heap":
            return httpx.Response(200, content=profile)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_capture_persists_private_checksum_pinned_artifact(tmp_path) -> None:
    store = RuntimeProfileStore(
        tmp_path,
        target_ports={
            "platform-relay-1": (8010, 11010),
            "platform-relay-2": (8011, 11011),
        },
        transport=_transport(),
    )
    artifact = await store.capture(
        target="platform-relay-1",
        profile_type="cpu",
        seconds=15,
        actor="operator@example.com",
        reason="investigate slow benchmark runs",
    )

    assert artifact.source_revision == _REVISION
    assert artifact.byte_size == len(b"pprof-profile")
    assert artifact.expires_at - artifact.created_at == timedelta(minutes=15)
    metadata = store.get(artifact.profile_id)
    downloaded, path = store.download(artifact.profile_id)
    assert metadata == downloaded == artifact
    assert path.read_bytes() == b"pprof-profile"
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(tmp_path / f"{artifact.profile_id}.json").st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_capture_rejects_oversized_profile(tmp_path) -> None:
    store = RuntimeProfileStore(
        tmp_path,
        transport=_transport(b"x" * (MAX_PROFILE_BYTES + 1)),
    )
    with pytest.raises(RuntimeProfileError, match="safety limit"):
        await store.capture(
            target="platform-relay-1",
            profile_type="heap",
            seconds=None,
            actor="operator@example.com",
            reason="inspect retained relay allocations",
        )


@pytest.mark.asyncio
async def test_capture_rejects_empty_profile(tmp_path) -> None:
    store = RuntimeProfileStore(tmp_path, transport=_transport(b""))
    with pytest.raises(RuntimeProfileError, match="empty cpu runtime profile"):
        await store.capture(
            target="platform-relay-1",
            profile_type="cpu",
            seconds=15,
            actor="operator@example.com",
            reason="investigate slow benchmark runs",
        )


@pytest.mark.asyncio
async def test_expired_profile_is_deleted_on_read(tmp_path) -> None:
    store = RuntimeProfileStore(tmp_path, transport=_transport())
    artifact = await store.capture(
        target="platform-relay-1",
        profile_type="heap",
        seconds=None,
        actor="operator@example.com",
        reason="inspect retained relay allocations",
    )
    expired = artifact.model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    (tmp_path / f"{artifact.profile_id}.json").write_text(expired.model_dump_json())

    with pytest.raises(RuntimeProfileNotFoundError):
        store.get(artifact.profile_id)
    assert not (tmp_path / f"{artifact.profile_id}.pb.gz").exists()


def test_capture_request_binds_duration_to_cpu_only() -> None:
    RuntimeProfileCaptureRequest(
        target="platform-relay-1",
        profile_type="cpu",
        seconds=15,
        reason="investigate slow benchmark runs",
        confirmation="CAPTURE RUNTIME PROFILE",
    )
    with pytest.raises(ValidationError, match="seconds is required"):
        RuntimeProfileCaptureRequest(
            target="platform-relay-1",
            profile_type="cpu",
            reason="investigate slow benchmark runs",
            confirmation="CAPTURE RUNTIME PROFILE",
        )
    with pytest.raises(ValidationError, match="only valid"):
        RuntimeProfileCaptureRequest(
            target="platform-relay-1",
            profile_type="heap",
            seconds=15,
            reason="inspect retained relay allocations",
            confirmation="CAPTURE RUNTIME PROFILE",
        )
