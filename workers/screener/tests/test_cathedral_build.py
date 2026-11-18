"""Cathedral sealed build backend: off by default, receipts, failure mapping."""

from __future__ import annotations

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
from ditto_screener.cathedral_build import build_command
from ditto_screener.config import ScreenerConfig, parse_screener_config_from_env
from ditto_screener.errors import ScreenerConfigError
from ditto_screener.policy import ScreeningOutcome

from .test_config import _base_env
from .test_gate import _gate_with, _ok_run, _screen, _valid_tar

_GOLDEN_SOURCE = "5ad58eb239239b70cf02dedd34a428864000d45394bc76920b5b71fd08fbfaa6"
_BUILDER_DIGEST = (
    "sha256:d2dc198f7d839eae26b5a9cb0e7cdc4e2c97d9cb4ea66dbeb0a4c0c7f0b165f8"
)
_GOLDEN_ARGV_SHA256 = "0629687de8d155d83e5eeb9c2186ebbbc7587c296bcef23c759a01567ffe8fed"
_SANDBOX = "sbx-123"


def _image_archive() -> tuple[bytes, str]:
    """A tar whose manifest names a config file hashed to the image ID."""
    config = b'{"architecture":"amd64"}'
    digest = hashlib.sha256(config).hexdigest()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in (
            (f"{digest}.json", config),
            ("manifest.json", json.dumps([{"Config": f"{digest}.json"}]).encode()),
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


def _config(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path, **overrides: Any
) -> ScreenerConfig:
    key = tmp_path / "key"
    key.write_text("secret-key\n")
    return make_config(
        build_backend="cathedral",
        cathedral_api_key_file=str(key),
        cathedral_receipt_dir=str(tmp_path / "receipts"),
        **overrides,
    )


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
        original(self, *args, **kwargs)

    monkeypatch.setattr(cathedral_build.CathedralBuilder, "__init__", init)


def test_golden_command_hash() -> None:
    argv = build_command(_GOLDEN_SOURCE)
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
    assert fake.exec_body["command"] == build_command(fake.source_sha256)
    assert fake.exec_body["timeout_seconds"] <= 1750
    assert loaded and loaded[0][0] == fake.image_id
    assert fake.deleted
    receipt_path = tmp_path / "receipts" / f"{fake.source_sha256}-{_SANDBOX}.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["schema"] == "cathedral-build-receipt-v1"
    assert receipt["recipe"]["command"] == build_command(fake.source_sha256)
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
