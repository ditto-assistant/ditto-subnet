"""Public replay receipts stay report-only and fail closed on missing probes."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

from ditto_screener import replay_main
from ditto_screener.config import ScreenerConfig
from ditto_screener.gate import BuildGate, BuiltImageArtifact
from ditto_screener.heartbeat import FleetRelease, HostSpecs, ScreenerHeartbeatRequest
from ditto_screener.policy import ScreeningOutcome
from ditto_screener.replay_api import ReplayApiClient
from ditto_screener.replay_main import ReplayHeartbeatPublisher
from ditto_screener.replay_public import PUBLIC_CHECKS, ReplayLease, ReplayPublicRunner


def _lease() -> ReplayLease:
    return ReplayLease(
        replay_id=uuid4(),
        agent_id=uuid4(),
        source_attempt_id=uuid4(),
        artifact_sha256="a" * 64,
        policy_version=13,
        image_upload_id=None,
        image_sha256=None,
        lease_deadline=datetime.now(UTC) + timedelta(minutes=30),
        status="running",
    )


class FakeApi:
    def __init__(self, lease: ReplayLease) -> None:
        self.lease = lease
        self.receipts: dict[str, dict[str, Any]] = {}
        self.finished: dict[str, Any] | None = None

    async def claim(self) -> dict[str, Any]:
        return self.lease.model_dump(mode="json")

    async def inputs(self, _replay_id: Any) -> dict[str, Any]:
        return {
            "replay": self.lease.model_dump(mode="json"),
            "bench_version": 13,
            "miner_hotkey": "5" * 47,
            "artifact_url": "https://example.test/artifact",
            "image_url": "https://example.test/image"
            if self.lease.image_upload_id is not None
            else None,
            "urls_expire_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }

    async def build_upload(self, _replay_id: Any, payload: Any) -> dict[str, Any]:
        assert payload["artifact_sha256"] == self.lease.artifact_sha256
        self.lease.image_sha256 = payload["image_sha256"]
        return {
            "upload_url": "https://storage.example.test/upload",
            "required_headers": {"Content-Type": "application/x-tar"},
        }

    async def build_verify(self, _replay_id: Any, payload: Any) -> dict[str, Any]:
        return {
            "image_sha256": payload["image_sha256"],
            "image_verified_at": datetime.now(UTC).isoformat(),
        }

    async def receipt(self, _replay_id: Any, payload: Any) -> dict[str, Any]:
        assert payload["artifact_sha256"] == self.lease.artifact_sha256
        self.receipts[payload["check_code"]] = payload
        return payload

    async def finish(self, _replay_id: Any, **payload: Any) -> dict[str, Any]:
        assert payload["image_sha256"] == self.lease.image_sha256
        self.finished = payload
        return payload


class FakeGate:
    def __init__(self, image_path: Path, *, omit_last: bool = False) -> None:
        self._config = SimpleNamespace(v13_runtime_receipts_mode="shadow")
        self.image_path = image_path
        self.omit_last = omit_last

    async def screen(self, **kwargs: Any) -> Any:
        assert kwargs["build_only"] is True
        assert kwargs["replay_runtime_probes"] is True
        assert kwargs["policy_version"] == 13
        await kwargs["record_archive_verification"]()
        if kwargs["preverified_image"] is not None:
            assert kwargs["publish_image"] is None
            path, image_id = kwargs["preverified_image"]
            assert Path(path).read_bytes() == self.image_path.read_bytes()
            assert image_id == "sha256:" + "b" * 64
            await kwargs["record_preverified_image"]()
        else:
            data = self.image_path.read_bytes()
            await kwargs["publish_image"](
                BuiltImageArtifact(
                    path=str(self.image_path),
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                    image_id="sha256:" + "b" * 64,
                    image_ref="replay-test",
                )
            )
        codes = sorted(PUBLIC_CHECKS - {"archive_sha", "build_image_digest"})
        if self.omit_last:
            codes.pop()
        for code in codes:
            await kwargs["record_runtime_verification"](code, "c" * 64)
        return SimpleNamespace(outcome=ScreeningOutcome.PASS)


async def test_public_replay_reports_only_complete_receipts(tmp_path: Path) -> None:
    lease = _lease()
    api = FakeApi(lease)
    image = tmp_path / "image.tar"
    image.write_bytes(b"isolated-image")
    uploads: list[httpx.Request] = []

    def upload(request: httpx.Request) -> httpx.Response:
        uploads.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upload)) as http:
        runner = ReplayPublicRunner(
            api=cast(ReplayApiClient, api),
            gate=cast(BuildGate, FakeGate(image)),
            http=http,
        )
        assert await runner.run_once() == lease.replay_id
    assert len(uploads) == 1 and uploads[0].content == b"isolated-image"
    assert set(api.receipts) == PUBLIC_CHECKS
    assert api.receipts["archive_sha"]["image_sha256"] is None
    assert all(
        receipt["image_sha256"] == hashlib.sha256(b"isolated-image").hexdigest()
        for code, receipt in api.receipts.items()
        if code != "archive_sha"
    )
    assert api.finished is not None and api.finished["status"] == "reported"


async def test_public_replay_reuses_exact_verified_image(tmp_path: Path) -> None:
    image = tmp_path / "image.tar"
    image.write_bytes(b"isolated-image")
    lease = _lease()
    lease.image_upload_id = uuid4()
    lease.image_sha256 = hashlib.sha256(image.read_bytes()).hexdigest()
    lease.image_size_bytes = image.stat().st_size
    lease.image_id = "sha256:" + "b" * 64
    lease.image_verified_at = datetime.now(UTC)
    api = FakeApi(lease)
    requests: list[httpx.Request] = []

    def download(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=image.read_bytes())

    async with httpx.AsyncClient(transport=httpx.MockTransport(download)) as http:
        runner = ReplayPublicRunner(
            api=cast(ReplayApiClient, api),
            gate=cast(BuildGate, FakeGate(image)),
            http=http,
        )
        await runner.run_lease(lease)
    assert len(requests) == 1 and requests[0].method == "GET"
    assert set(api.receipts) == PUBLIC_CHECKS
    assert all(
        receipt["image_upload_id"] == lease.image_upload_id
        for receipt in api.receipts.values()
    )
    assert api.finished is not None and api.finished["status"] == "reported"


async def test_public_replay_refuses_changed_verified_image(tmp_path: Path) -> None:
    image = tmp_path / "image.tar"
    image.write_bytes(b"isolated-image")
    lease = _lease()
    lease.image_upload_id = uuid4()
    lease.image_sha256 = "f" * 64
    lease.image_size_bytes = image.stat().st_size
    lease.image_id = "sha256:" + "b" * 64
    lease.image_verified_at = datetime.now(UTC)
    api = FakeApi(lease)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=image.read_bytes())
        )
    ) as http:
        runner = ReplayPublicRunner(
            api=cast(ReplayApiClient, api),
            gate=cast(BuildGate, FakeGate(image)),
            http=http,
        )
        await runner.run_lease(lease)
    assert not api.receipts
    assert api.finished is not None
    assert api.finished["status"] == "failed"
    assert api.finished["failure_code"] == "verified-image-replay-download-failed"


def test_preverified_image_tar_must_match_pinned_config_id(tmp_path: Path) -> None:
    config_bytes = b'{"rootfs":{"type":"layers","diff_ids":[]}}'
    digest = hashlib.sha256(config_bytes).hexdigest()
    path = tmp_path / "portable.tar"
    manifest = json.dumps(
        [{"Config": f"{digest}.json", "RepoTags": None, "Layers": []}]
    ).encode()
    with tarfile.open(path, "w") as archive:
        for name, data in (
            ("manifest.json", manifest),
            (f"{digest}.json", config_bytes),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    assert BuildGate._replay_image_config_matches(str(path), f"sha256:{digest}")
    assert not BuildGate._replay_image_config_matches(str(path), "sha256:" + "f" * 64)


async def test_public_replay_fails_if_one_runtime_probe_is_missing(
    tmp_path: Path,
) -> None:
    lease = _lease()
    api = FakeApi(lease)
    image = tmp_path / "image.tar"
    image.write_bytes(b"isolated-image")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    ) as http:
        runner = ReplayPublicRunner(
            api=cast(ReplayApiClient, api),
            gate=cast(BuildGate, FakeGate(image, omit_last=True)),
            http=http,
        )
        await runner.run_lease(lease)
    assert api.finished is not None
    assert api.finished["status"] == "failed"
    assert api.finished["failure_code"] == "replay-public-receipts-incomplete"
    assert set(api.receipts) != PUBLIC_CHECKS


async def test_public_replay_finishes_against_pinned_image_after_put_failure(
    tmp_path: Path,
) -> None:
    lease = _lease()
    api = FakeApi(lease)
    image = tmp_path / "image.tar"
    image.write_bytes(b"isolated-image")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(503))
    ) as http:
        runner = ReplayPublicRunner(
            api=cast(ReplayApiClient, api),
            gate=cast(BuildGate, FakeGate(image)),
            http=http,
        )
        await runner.run_lease(lease)
    assert api.finished is not None
    assert api.finished["status"] == "failed"
    assert api.finished["image_sha256"] == hashlib.sha256(b"isolated-image").hexdigest()


async def test_replay_heartbeat_attests_exact_process_and_release(
    make_config: Callable[..., ScreenerConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        replay_main,
        "collect_host_specs",
        lambda: HostSpecs(
            cpu_count=8,
            cpu_physical_cores=4,
            memory_total_mib=16384,
            disk_total_gib=100,
            architecture="x86_64",
        ),
    )
    monkeypatch.setattr(
        replay_main,
        "collect_fleet_release",
        lambda **_kwargs: FleetRelease(builtin_policy_version=13, version="0.999.0"),
    )
    heartbeats: list[ScreenerHeartbeatRequest] = []

    class HeartbeatApi:
        async def heartbeat(self, request: ScreenerHeartbeatRequest) -> Any:
            heartbeats.append(request)
            return SimpleNamespace(accepted=True)

    class Keypair:
        def sign(self, _message: bytes) -> bytes:
            return b"\x00" * 64

    publisher = ReplayHeartbeatPublisher(
        config=make_config(),
        keypair=Keypair(),
        api=cast(ReplayApiClient, HeartbeatApi()),
    )
    await publisher.publish()
    assert len(heartbeats) == 1
    assert heartbeats[0].instance_id == "subnet-screener-2-worker-1"
    assert heartbeats[0].protocol_version == 7
    assert heartbeats[0].policy_version == 13
    assert heartbeats[0].release is not None
    assert heartbeats[0].release.version == "0.999.0"
