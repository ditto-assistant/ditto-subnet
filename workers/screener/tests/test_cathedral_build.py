"""Cathedral sealed build backend: off by default, receipts, failure mapping."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from ditto_screener import cathedral_build
from ditto_screener import gate as gate_module
from ditto_screener.cathedral_build import (
    CathedralBuilder,
    CathedralBuildSettings,
    CathedralInfrastructureError,
    build_command,
    merge_evidence_pages,
)
from ditto_screener.config import ScreenerConfig, parse_screener_config_from_env
from ditto_screener.errors import ScreenerConfigError
from ditto_screener.gate import BuildGate, LeaseDeadline
from ditto_screener.policy import ScreeningOutcome

from .test_config import _base_env
from .test_gate import _gate_with, _ok_run, _screen, _valid_tar

_GOLDEN_SOURCE = "5ad58eb239239b70cf02dedd34a428864000d45394bc76920b5b71fd08fbfaa6"
_BUILDER_DIGEST = (
    "sha256:d2dc198f7d839eae26b5a9cb0e7cdc4e2c97d9cb4ea66dbeb0a4c0c7f0b165f8"
)
_GOLDEN_ARGV_SHA256 = "c2472fc067f226d26398ad8db3836acf1fa66cec5fc9b0994b8b8eaad68c681a"
_SANDBOX = "sbx-123"


def _image_archive() -> tuple[bytes, str]:
    """An OCI-layout archive as ``docker image save`` writes on docker 27.

    Config and layer live under ``blobs/sha256/``; ``manifest.json`` points at
    them; ``index.json`` and ``oci-layout`` are the envelope.
    """
    layer_buffer = io.BytesIO()
    with tarfile.open(fileobj=layer_buffer, mode="w") as layer:
        payload = b"hello"
        info = tarfile.TarInfo("hello.txt")
        info.size = len(payload)
        layer.addfile(info, io.BytesIO(payload))
    layer_bytes = layer_buffer.getvalue()
    layer_hex = hashlib.sha256(layer_bytes).hexdigest()
    config = json.dumps(
        {
            "architecture": "amd64",
            "os": "linux",
            "config": {},
            "rootfs": {"type": "layers", "diff_ids": [f"sha256:{layer_hex}"]},
        }
    ).encode()
    digest = hashlib.sha256(config).hexdigest()
    manifest = json.dumps(
        [
            {
                "Config": f"blobs/sha256/{digest}",
                "RepoTags": None,
                "Layers": [f"blobs/sha256/{layer_hex}"],
            }
        ]
    ).encode()
    index = json.dumps(
        {"schemaVersion": 2, "manifests": [{"digest": f"sha256:{digest}"}]}
    ).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in (
            ("blobs/sha256/" + layer_hex, layer_bytes),
            ("blobs/sha256/" + digest, config),
            ("index.json", index),
            ("manifest.json", manifest),
            ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue(), f"sha256:{digest}"


class FakeCathedral:
    """A fake Cathedral API recording every call."""

    def __init__(self, source: bytes, **options: Any) -> None:
        self.source = source
        self.source_sha256 = hashlib.sha256(source).hexdigest()
        self.options = options
        self.archive, self.image_id = _image_archive()
        half = len(self.archive) // 2
        self.parts = {
            "image.tar.part-aaa": self.archive[:half],
            "image.tar.part-aab": self.archive[half:],
        }
        self.calls: list[tuple[str, str]] = []
        self.create_headers: httpx.Headers | None = None
        self.create_body: dict[str, Any] = {}
        self.put_body: bytes | None = None
        self.exec_body: dict[str, Any] = {}
        self.deleted = False

    def result_json(self) -> bytes:
        parts = [
            {"name": n, "sha256": hashlib.sha256(d).hexdigest(), "size": len(d)}
            for n, d in self.parts.items()
        ]
        return json.dumps(
            {
                "schema": "cathedral-docker-build-result-v1",
                "source_sha256": self.source_sha256,
                "image_id": self.image_id,
                "archive_sha256": hashlib.sha256(self.archive).hexdigest(),
                "archive_size": len(self.archive),
                "parts": parts,
                "docker_version": "27.0",
                "buildx_version": "v0.1",
            }
        ).encode()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.calls.append((method, path))
        if method in {"POST", "PUT", "DELETE"} and not request.headers.get(
            "Idempotency-Key"
        ):
            # Cathedral prod answers 422 to a write without one.
            return httpx.Response(422, json={"detail": "Idempotency-Key required"})
        if "transport" in self.options and path.endswith("/execs"):
            raise httpx.ConnectError("boom")
        if path.endswith("/execs/ex1") and self.options.get("poll_drops", 0) > 0:
            # A long poll whose connection drops (seen once on Cathedral prod).
            self.options["poll_drops"] -= 1
            raise httpx.ReadError("connection dropped")
        if method == "POST" and path == "/v1/sandboxes":
            if self.options.get("create_status"):
                return httpx.Response(self.options["create_status"], json={})
            self.create_headers = request.headers
            self.create_body = json.loads(request.content)
            return httpx.Response(201, json={"id": _SANDBOX, "state": "creating"})
        if method == "GET" and path == f"/v1/sandboxes/{_SANDBOX}":
            state = "deleted" if self.deleted else "running"
            digest = self.options.get("image_digest", _BUILDER_DIGEST)
            return httpx.Response(200, json={"state": state, "image_digest": digest})
        if method == "DELETE":
            if self.options.get("delete_fail_times", 0) > 0:
                # A gateway blip on the delete (seen on Cathedral prod).
                self.options["delete_fail_times"] -= 1
                return httpx.Response(502, json={})
            if self.options.get("delete_status"):
                return httpx.Response(self.options["delete_status"], json={})
            self.deleted = True
            return httpx.Response(200, json={})
        if method == "PUT" and path.endswith("/files"):
            self.put_body = request.content
            assert request.url.params["path"] == "/work/in/src.tgz"
            return httpx.Response(200, json={})
        if method == "POST" and path.endswith("/execs"):
            self.exec_body = json.loads(request.content)
            return httpx.Response(202, json={"exec_id": "ex1"})
        if method == "GET" and path.endswith("/execs/ex1"):
            if self.options.get("running_polls", 0) > 0:
                self.options["running_polls"] -= 1
                if callback := self.options.get("on_poll"):
                    callback()
                return httpx.Response(
                    200, json={"state": "running", "exit_code": None, "stdout": ""}
                )
            return httpx.Response(
                200,
                json={
                    "state": self.options.get("exec_state", "exited"),
                    "exit_code": self.options.get("exit_code", 0),
                    "stdout": self.options.get("stdout", ""),
                },
            )
        if method == "GET" and path.endswith("/files"):
            name = request.url.params["path"].rsplit("/", 1)[1]
            if name == "result.json":
                return httpx.Response(200, content=self.result_json())
            data = self.parts[name]
            if self.options.get("corrupt_part") == name:
                data = b"x" * len(data)
            return httpx.Response(200, content=data)
        if method == "GET" and path.endswith("/evidence"):
            return httpx.Response(
                200,
                json={
                    "schema": "cathedral_sandbox_evidence_v1",
                    "sandbox_id": _SANDBOX,
                    "cathedral_receipts": {"receipts": [{"n": 1}], "next_after": None},
                    "guest_receipts": {"receipts": [{"receipt_id": "g1"}]},
                },
            )
        return httpx.Response(404)


def calls_tag(calls: list[list[str]]) -> str:
    """The build tag the gate tore down (``rmi -f <tag>``)."""
    return next(c[2] for c in calls if c[:2] == ["rmi", "-f"])


def _config(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path, **overrides: Any
) -> ScreenerConfig:
    key = tmp_path / "key"
    key.write_text("secret-key\n")
    settings: dict[str, Any] = {
        "build_backend": "cathedral",
        "cathedral_api_key_file": str(key),
        "cathedral_receipt_dir": str(tmp_path / "receipts"),
    }
    return make_config(**{**settings, **overrides})


def _wire(gate: Any, fake: FakeCathedral) -> list[tuple[str, int]]:
    # No real sleeping between polls.
    gate._cathedral_client = httpx.AsyncClient(
        transport=httpx.MockTransport(fake.handler)
    )
    loaded: list[tuple[str, int]] = []

    async def load(path: str, tag: str, **_: Any) -> Any:
        loaded.append((tag, Path(path).stat().st_size))
        return True, "", tag

    gate._load_remote_image = load
    return loaded


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    original = cathedral_build.CathedralBuilder.__init__

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs["poll_interval"] = 0.0
        kwargs["retry_delay"] = 0.0
        kwargs["delete_timeout"] = 0.05
        original(self, *args, **kwargs)

    monkeypatch.setattr(cathedral_build.CathedralBuilder, "__init__", init)


def test_golden_command_hash() -> None:
    argv = build_command(
        _GOLDEN_SOURCE, memory="7g", part_bytes=67108864, build_timeout=1700
    )
    canonical = json.dumps(
        argv, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    assert hashlib.sha256(canonical).hexdigest() == _GOLDEN_ARGV_SHA256


async def test_backend_off_builds_locally_with_no_http(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()
    calls: list[list[str]] = []
    gate = _gate_with(make_config(), _ok_run(calls), tarball=tarball)
    seen: list[httpx.Request] = []
    gate._cathedral_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(500))
    )
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())
    assert result.outcome == ScreeningOutcome.PASS
    assert any(c[:2] == ["buildx", "build"] for c in calls)
    assert seen == []


async def test_success_path_saves_receipt_and_loads_image(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    tarball = _valid_tar()
    fake = FakeCathedral(tarball)
    calls: list[list[str]] = []
    gate = _gate_with(_config(make_config, tmp_path), _ok_run(calls), tarball=tarball)
    loaded = _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256, build_only=True)
    assert result.outcome == ScreeningOutcome.PASS
    assert not any(c[:2] == ["buildx", "build"] for c in calls)
    assert fake.create_body["required_hardware"] == "amd_sev_snp"
    assert fake.create_body["image"] == "docker:27-dind"
    assert fake.create_body["template"] == "sealed-4x8"
    assert fake.create_headers is not None
    assert fake.create_headers["authorization"] == "Bearer secret-key"
    assert re.fullmatch(r"[0-9a-f]{64}", fake.create_headers["idempotency-key"])
    assert fake.put_body == tarball
    assert fake.exec_body["command"] == build_command(
        fake.source_sha256, build_timeout=60
    )
    assert fake.exec_body["timeout_seconds"] == 60 + 45
    assert loaded and loaded[0][0] == fake.image_id
    assert fake.deleted
    receipt_path = tmp_path / "receipts" / f"{fake.source_sha256}-{_SANDBOX}.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["schema"] == "cathedral-build-receipt-v1"
    assert receipt["recipe"]["command"] == build_command(
        fake.source_sha256, build_timeout=60
    )
    assert receipt["recipe"]["build_timeout"] == 60
    assert receipt["result"]["image_id"] == fake.image_id
    assert receipt["evidence"]["complete"] is True
    assert "secret-key" not in receipt_path.read_text()
    order = [m for m, _ in fake.calls]
    assert order.index("DELETE") > order.index("PUT")


async def test_exit_91_is_a_docker_build_reject(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    tarball = _valid_tar()
    fake = FakeCathedral(tarball, exit_code=91, stdout="error: bad Dockerfile")
    gate = _gate_with(_config(make_config, tmp_path), _ok_run(), tarball=tarball)
    _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256)
    assert result.outcome == ScreeningOutcome.DETERMINISTIC_REJECT
    assert result.evidence[-1].code == "docker-build"
    assert "bad Dockerfile" in result.detail
    assert fake.deleted


@pytest.mark.parametrize(
    "options",
    [
        {"exec_state": "timed_out", "exit_code": None},
        {"exit_code": 1},
        {"create_status": 503},
        {"transport": True},
        {"corrupt_part": "image.tar.part-aab"},
        {"image_digest": "sha256:" + "0" * 64},
    ],
)
async def test_cathedral_failures_are_retryable_infra(
    make_config: Callable[..., ScreenerConfig],
    tmp_path: Path,
    options: dict[str, Any],
) -> None:
    tarball = _valid_tar()
    fake = FakeCathedral(tarball, **options)
    gate = _gate_with(_config(make_config, tmp_path), _ok_run(), tarball=tarball)
    loaded = _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256)
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert result.evidence[-1].code == "cathedral-build-infrastructure"
    assert result.evidence[-1].code not in {"docker-build", "docker-build-timeout"}
    assert loaded == []
    if "create_status" not in options:
        assert fake.deleted, "room must be deleted even when the build fails"
    else:
        assert ("DELETE", f"/v1/sandboxes/{_SANDBOX}") not in fake.calls


def test_invalid_backend_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("SCREENER_BUILD_BACKEND", "kaniko")
    with pytest.raises(ScreenerConfigError, match="SCREENER_BUILD_BACKEND"):
        parse_screener_config_from_env()


def test_cathedral_without_key_file_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.delenv("SCREENER_CATHEDRAL_API_KEY_FILE", raising=False)
    monkeypatch.setenv("SCREENER_BUILD_BACKEND", "cathedral")
    with pytest.raises(ScreenerConfigError, match="API_KEY_FILE"):
        parse_screener_config_from_env()


def test_cathedral_config_defaults_and_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base_env(monkeypatch)
    assert parse_screener_config_from_env().build_backend == "local"
    monkeypatch.setenv("SCREENER_BUILD_BACKEND", "cathedral")
    monkeypatch.setenv("SCREENER_CATHEDRAL_API_KEY_FILE", "/run/secrets/cathedral")
    cfg = parse_screener_config_from_env()
    assert cfg.cathedral_api_url == "https://api.polaris.computer"
    assert cfg.cathedral_template == "sealed-4x8"
    assert cfg.cathedral_builder_image == "docker:27-dind"
    assert cfg.cathedral_build_memory == "7g"
    assert cfg.cathedral_max_spend_usd == 0.5
    monkeypatch.setenv("SCREENER_CATHEDRAL_BUILDER_IMAGE", "alpine:3")
    with pytest.raises(ScreenerConfigError, match="BUILDER_IMAGE"):
        parse_screener_config_from_env()
    monkeypatch.setenv("SCREENER_CATHEDRAL_BUILDER_IMAGE", "docker:27-dind")
    monkeypatch.setenv("SCREENER_CATHEDRAL_BUILD_MEMORY", "7g; reboot")
    with pytest.raises(ScreenerConfigError, match="BUILD_MEMORY"):
        parse_screener_config_from_env()


async def test_dropped_exec_poll_is_retried_not_infra(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    tarball = _valid_tar()
    fake = FakeCathedral(tarball, poll_drops=2)
    gate = _gate_with(_config(make_config, tmp_path), _ok_run(), tarball=tarball)
    loaded = _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256, build_only=True)
    assert result.outcome == ScreeningOutcome.PASS
    assert loaded and loaded[0][0] == fake.image_id


async def test_exec_poll_that_keeps_dropping_is_infra(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    tarball = _valid_tar()
    fake = FakeCathedral(tarball, poll_drops=10)
    gate = _gate_with(_config(make_config, tmp_path), _ok_run(), tarball=tarball)
    _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256)
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert result.evidence[-1].code == "cathedral-build-infrastructure"
    assert fake.deleted


# --- review r1 fixes -------------------------------------------------------


def _docker_run(
    image_id: str, calls: list[list[str]], *, volumes: str = ""
) -> Callable[..., Any]:
    """A docker stub that answers load, inspect (id and volumes) and tag."""

    async def run(args: list[str], **_: Any) -> tuple[int, str]:
        calls.append(args)
        if args[:4] == ["image", "inspect", "--format", "{{.Id}}"]:
            return 0, image_id
        if args[:3] == ["image", "inspect", "--format"]:
            return 0, volumes
        return 0, ""

    return run


def _wire_client(gate: Any, fake: FakeCathedral) -> None:
    gate._cathedral_client = httpx.AsyncClient(
        transport=httpx.MockTransport(fake.handler)
    )


async def test_oci_archive_is_normalized_and_really_loaded(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    """B1: nothing is stubbed after the download, including the config check."""
    tarball = _valid_tar()
    fake = FakeCathedral(tarball)
    calls: list[list[str]] = []
    gate = _gate_with(
        _config(make_config, tmp_path),
        _docker_run(fake.image_id, calls),
        tarball=tarball,
    )
    _wire_client(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256, build_only=True)
    assert result.outcome == ScreeningOutcome.PASS
    load = next(c for c in calls if c[:2] == ["image", "load"])
    # The loaded file is a temp copy, gone after the build.
    assert not Path(load[3]).exists()
    tag = next(c for c in calls if c[:2] == ["image", "tag"])
    assert tag[2] == fake.image_id
    assert tag[3] == calls_tag(calls)


def test_real_config_check_accepts_normalized_and_refuses_raw_oci(
    tmp_path: Path,
) -> None:
    archive, image_id = _image_archive()
    raw = tmp_path / "raw.tar"
    raw.write_bytes(archive)
    portable = tmp_path / "portable.tar"
    normalized = BuildGate._portable_image_archive(
        str(raw), str(portable), deadline=None
    )
    assert normalized.image_id == image_id
    assert BuildGate._replay_image_config_matches(str(portable), image_id)
    # The raw docker 27 envelope is what failed every build before the fix.
    assert not BuildGate._replay_image_config_matches(str(raw), image_id)
    assert not BuildGate._replay_image_config_matches(
        str(portable), "sha256:" + "0" * 64
    )


async def test_archive_for_another_image_id_is_infra(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    tarball = _valid_tar()
    fake = FakeCathedral(tarball)
    real_result = fake.result_json

    def other_id() -> bytes:
        body = json.loads(real_result())
        body["image_id"] = "sha256:" + "ab" * 32
        return json.dumps(body).encode()

    fake.result_json = other_id  # type: ignore[method-assign]
    gate = _gate_with(_config(make_config, tmp_path), _ok_run(), tarball=tarball)
    loaded = _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256)
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert result.evidence[-1].code == "cathedral-build-infrastructure"
    assert loaded == []
    assert fake.deleted


async def test_exit_92_with_full_limit_is_docker_build_timeout(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    tarball = _valid_tar()
    fake = FakeCathedral(tarball, exit_code=92, stdout="hung")
    gate = _gate_with(_config(make_config, tmp_path), _ok_run(), tarball=tarball)
    _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256)
    assert result.outcome == ScreeningOutcome.DETERMINISTIC_REJECT
    assert result.evidence[-1].code == "docker-build-timeout"
    assert "[timeout after 60s]" in result.detail
    assert fake.deleted


async def test_exit_92_under_cathedrals_cap_is_infra(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    """Ditto's limit (2700 s) is above what Cathedral can give (1700 s)."""
    tarball = _valid_tar()
    fake = FakeCathedral(tarball, exit_code=92)
    gate = _gate_with(
        _config(make_config, tmp_path, build_timeout_seconds=2700.0),
        _ok_run(),
        tarball=tarball,
    )
    _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256)
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert result.evidence[-1].code == "cathedral-build-infrastructure"
    assert fake.exec_body["timeout_seconds"] == 1745
    assert "timeout 1700 docker buildx" in fake.exec_body["command"][-1]
    assert fake.deleted


async def test_short_lease_does_not_cap_the_exec(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    tarball = _valid_tar()
    fake = FakeCathedral(tarball)
    gate = _gate_with(
        _config(make_config, tmp_path, build_timeout_seconds=2700.0),
        _ok_run(),
        tarball=tarball,
    )
    _wire(gate, fake)
    deadline = LeaseDeadline(asyncio.get_running_loop().time() + 570)
    async with gate._client:
        result = await _screen(
            gate, fake.source_sha256, build_only=True, deadline=deadline
        )
    assert result.outcome == ScreeningOutcome.PASS
    assert fake.exec_body["timeout_seconds"] == 1745


async def test_lease_lapsing_mid_build_is_retryable_and_deletes_room(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    tarball = _valid_tar()
    deadline = LeaseDeadline(asyncio.get_running_loop().time() + 570)

    def lapse() -> None:
        deadline.expires_at = asyncio.get_running_loop().time() - 1

    fake = FakeCathedral(tarball, running_polls=2, on_poll=lapse)
    gate = _gate_with(_config(make_config, tmp_path), _ok_run(), tarball=tarball)
    loaded = _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256, deadline=deadline)
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert result.evidence[-1].code == "docker-build-infrastructure"
    assert "lease expired" in result.detail
    assert loaded == []
    assert fake.deleted


async def test_lease_renewed_mid_build_keeps_building(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    tarball = _valid_tar()
    deadline = LeaseDeadline(asyncio.get_running_loop().time() + 570)

    def renew() -> None:
        deadline.renew(asyncio.get_running_loop().time() + 570)

    fake = FakeCathedral(tarball, running_polls=3, on_poll=renew)
    gate = _gate_with(_config(make_config, tmp_path), _ok_run(), tarball=tarball)
    _wire(gate, fake)
    async with gate._client:
        result = await _screen(
            gate, fake.source_sha256, build_only=True, deadline=deadline
        )
    assert result.outcome == ScreeningOutcome.PASS


async def test_declared_volume_is_a_docker_build_reject(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    """H2: the real ``_load_remote_image`` sees VOLUME and the miner is rejected."""
    tarball = _valid_tar()
    fake = FakeCathedral(tarball)
    calls: list[list[str]] = []
    gate = _gate_with(
        _config(make_config, tmp_path),
        _docker_run(fake.image_id, calls, volumes="declared"),
        tarball=tarball,
    )
    _wire_client(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256)
    assert result.outcome == ScreeningOutcome.DETERMINISTIC_REJECT
    assert result.evidence[-1].code == "docker-build"
    assert "writable volumes" in result.detail
    assert fake.deleted
    # Tagged before the VOLUME check, so teardown's rmi removes the image.
    tag = next(c for c in calls if c[:2] == ["image", "tag"])
    assert tag[3] == calls_tag(calls)
    assert calls.index(tag) < calls.index(["rmi", "-f", tag[3]])


async def test_image_load_failure_stays_infra(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    tarball = _valid_tar()
    fake = FakeCathedral(tarball)

    async def run(args: list[str], **_: Any) -> tuple[int, str]:
        return (1, "boom") if args[:2] == ["image", "load"] else (0, "")

    gate = _gate_with(_config(make_config, tmp_path), run, tarball=tarball)
    _wire_client(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256)
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert result.evidence[-1].code == "cathedral-build-infrastructure"


async def test_filesystem_error_is_cathedral_infra_and_room_deleted(
    make_config: Callable[..., ScreenerConfig],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def full(_: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cathedral_build.shutil, "disk_usage", full)
    tarball = _valid_tar()
    fake = FakeCathedral(tarball)
    gate = _gate_with(_config(make_config, tmp_path), _ok_run(), tarball=tarball)
    loaded = _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256)
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert result.evidence[-1].code == "cathedral-build-infrastructure"
    assert loaded == []
    assert fake.deleted


@pytest.mark.parametrize("exit_code", [90, 137])
async def test_other_exit_codes_are_infra(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path, exit_code: int
) -> None:
    tarball = _valid_tar()
    fake = FakeCathedral(tarball, exit_code=exit_code)
    gate = _gate_with(_config(make_config, tmp_path), _ok_run(), tarball=tarball)
    _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256)
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert result.evidence[-1].code == "cathedral-build-infrastructure"
    assert fake.deleted


async def test_exit_91_with_oom_marker_is_docker_build_infrastructure(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    tarball = _valid_tar()
    marker = next(
        m for m in gate_module._DOCKER_INFRASTRUCTURE_MARKERS if "oom" in m.lower()
    )
    fake = FakeCathedral(tarball, exit_code=91, stdout=f"error: {marker}")
    gate = _gate_with(_config(make_config, tmp_path), _ok_run(), tarball=tarball)
    _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256)
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert result.evidence[-1].code == "docker-build-infrastructure"


@pytest.mark.parametrize("options", [{"delete_status": 500}])
async def test_room_delete_failure_does_not_fail_the_build(
    make_config: Callable[..., ScreenerConfig],
    tmp_path: Path,
    options: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    tarball = _valid_tar()
    fake = FakeCathedral(tarball, **options)
    gate = _gate_with(_config(make_config, tmp_path), _ok_run(), tarball=tarball)
    _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256, build_only=True)
    assert result.outcome == ScreeningOutcome.PASS
    assert "not confirmed deleted" in caplog.text


async def test_receipt_save_failure_does_not_fail_the_build(
    make_config: Callable[..., ScreenerConfig],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tarball = _valid_tar()
    fake = FakeCathedral(tarball)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    gate = _gate_with(
        _config(make_config, tmp_path, cathedral_receipt_dir=str(blocker / "r")),
        _ok_run(),
        tarball=tarball,
    )
    _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256, build_only=True)
    assert result.outcome == ScreeningOutcome.PASS
    assert "was not saved" in caplog.text
    assert fake.deleted


async def test_cancellation_mid_build_still_deletes_the_room(tmp_path: Path) -> None:
    tarball = _valid_tar()
    source = tmp_path / "src.tgz"
    source.write_bytes(tarball)
    fake = FakeCathedral(tarball, running_polls=10**6)

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        return fake.handler(request)

    key = tmp_path / "key"
    key.write_text("secret-key\n")
    settings = CathedralBuildSettings(
        api_url="https://cathedral.test",
        api_key_file=str(key),
        template="sealed-4x8",
        builder_image="docker:27-dind",
        builder_digest=_BUILDER_DIGEST,
        memory="7g",
        max_spend_usd=0.5,
        receipt_dir=str(tmp_path / "receipts"),
    )
    builder = CathedralBuilder(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        poll_interval=0.0,
        retry_delay=0.0,
        delete_timeout=0.05,
    )
    task = asyncio.ensure_future(
        builder.build(
            str(source),
            agent_id="a",
            attempt_id="b",
            timeout=60,
            max_archive_bytes=1 << 30,
            disk_reserve_bytes=0,
        )
    )
    fake.options["on_poll"] = lambda: task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.deleted


def _page(
    *, n: list[int], next_after: int | None, guest: list[str], next_offset: int | None
) -> dict[str, Any]:
    return {
        "schema": "cathedral_sandbox_evidence_v1",
        "sandbox_id": _SANDBOX,
        "cathedral_receipts": {
            "receipts": [{"n": i} for i in n],
            "next_after": next_after,
            "head": n[-1] if n else None,
        },
        "guest_receipts": {
            "receipts": [{"receipt_id": g} for g in guest],
            "next_offset": next_offset,
            "count": len(guest),
        },
    }


def test_evidence_merges_across_two_pages() -> None:
    pages = {
        (0, 0): _page(n=[1, 2], next_after=2, guest=["g1"], next_offset=1),
        (2, 1): _page(n=[3], next_after=None, guest=["g2"], next_offset=None),
    }
    merged = merge_evidence_pages(lambda a, o: pages[(a, o)], _SANDBOX)
    assert [r["n"] for r in merged["cathedral_receipts"]["receipts"]] == [1, 2, 3]
    assert [r["receipt_id"] for r in merged["guest_receipts"]["receipts"]] == [
        "g1",
        "g2",
    ]
    assert merged["complete"] is True


def test_evidence_pages_that_do_not_move_are_refused() -> None:
    stuck = _page(n=[1], next_after=1, guest=[], next_offset=None)
    with pytest.raises(CathedralInfrastructureError, match="did not move"):
        merge_evidence_pages(lambda _a, _o: stuck, _SANDBOX)


def test_evidence_with_too_many_pages_is_refused() -> None:
    counter = iter(range(1, 10**6))

    def fetch(_after: int, _offset: int) -> dict[str, Any]:
        n = next(counter)
        return _page(n=[n], next_after=n, guest=[], next_offset=None)

    with pytest.raises(CathedralInfrastructureError, match="too many pages"):
        merge_evidence_pages(fetch, _SANDBOX)


def test_build_command_rejects_a_bad_build_timeout() -> None:
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="build_timeout"):
            build_command(_GOLDEN_SOURCE, build_timeout=bad)  # type: ignore[arg-type]
    script = build_command(_GOLDEN_SOURCE, build_timeout=900)[-1]
    assert "timeout 900 docker buildx build" in script
    assert "-ge 900 ] && exit 92; exit 91" in script


async def test_room_delete_is_resent_after_a_gateway_error(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    tarball = _valid_tar()
    fake = FakeCathedral(tarball, delete_fail_times=2)
    gate = _gate_with(_config(make_config, tmp_path), _ok_run(), tarball=tarball)
    _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256, build_only=True)
    assert result.outcome == ScreeningOutcome.PASS
    assert fake.deleted, "a 502 on DELETE must not leave the room running"
    assert [c for c in fake.calls if c[0] == "DELETE"] == [
        ("DELETE", f"/v1/sandboxes/{_SANDBOX}")
    ] * 3


async def test_room_reported_oversized_image_is_a_miner_reject(
    make_config: Callable[..., ScreenerConfig],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tarball = _valid_tar()
    fake = FakeCathedral(tarball)
    monkeypatch.setattr(gate_module, "_MAX_SCREENED_IMAGE_BYTES", len(fake.archive) - 1)
    gate = _gate_with(_config(make_config, tmp_path), _ok_run(), tarball=tarball)
    loaded = _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256)
    assert result.outcome == ScreeningOutcome.DETERMINISTIC_REJECT
    assert result.evidence[-1].code == "screened-image-too-large"
    assert loaded == []
    assert fake.deleted
    # No part was downloaded.
    assert not any("image.tar.part" in p for _, p in fake.calls)


async def test_oversized_image_found_while_normalizing_is_a_miner_reject(
    make_config: Callable[..., ScreenerConfig],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def too_big(*_: Any, **__: Any) -> Any:
        raise gate_module._ScreenedImageTooLargeError("archive expands beyond the cap")

    monkeypatch.setattr(BuildGate, "_portable_image_archive", staticmethod(too_big))
    tarball = _valid_tar()
    fake = FakeCathedral(tarball)
    gate = _gate_with(_config(make_config, tmp_path), _ok_run(), tarball=tarball)
    loaded = _wire(gate, fake)
    async with gate._client:
        result = await _screen(gate, fake.source_sha256)
    assert result.outcome == ScreeningOutcome.DETERMINISTIC_REJECT
    assert result.evidence[-1].code == "screened-image-too-large"
    assert loaded == []
    assert fake.deleted


@pytest.mark.parametrize(
    "kwargs",
    [
        {"memory": "7g; reboot"},
        {"memory": "0g"},
        {"memory": "7"},
        {"memory": "1234567g"},
        {"memory": "7G"},
        {"memory": "7g\n"},
        {"part_bytes": (1 << 20) - 1},
        {"part_bytes": (2 << 30) + 1},
        {"part_bytes": True},
        {"part_bytes": 1.5},
    ],
)
def test_build_command_refuses_forged_memory_and_part_bytes(
    kwargs: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        build_command(_GOLDEN_SOURCE, **kwargs)


def test_build_command_accepts_the_range_edges() -> None:
    build_command(_GOLDEN_SOURCE, memory="999999m", part_bytes=1 << 20)
    build_command(_GOLDEN_SOURCE, memory="1g", part_bytes=2 << 30)
