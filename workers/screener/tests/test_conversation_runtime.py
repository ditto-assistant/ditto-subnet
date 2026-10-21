import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from ditto_screener.conversation import AssessmentFailure
from ditto_screener.conversation_runtime import ConversationRuntime
from ditto_screening_protocol.conversation import ConversationLaunch


def fixture_image(layer: bytes | None = None, *, tampered: bool = False):
    diff_ids = ["sha256:" + hashlib.sha256(layer).hexdigest()] if layer else []
    config = json.dumps({"rootfs": {"type": "layers", "diff_ids": diff_ids}}).encode()
    digest = "sha256:" + hashlib.sha256(config).hexdigest()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in (
            ("config.json", config),
            (
                "manifest.json",
                json.dumps(
                    [
                        {
                            "Config": "config.json",
                            "RepoTags": None,
                            "Layers": ["layer.tar"] if layer else [],
                        }
                    ]
                ).encode(),
            ),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if layer:
            data = b"different filesystem bytes" if tampered else layer
            info = tarfile.TarInfo("layer.tar")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return stream.getvalue(), digest


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [None, "archive_digest", "image_identity"])
async def test_verified_image_is_fresh_isolated_and_never_receives_provider_key(
    tmp_path, monkeypatch, tamper
):
    archive, image_id = fixture_image()
    launch = ConversationLaunch(
        assessment_id=uuid4(),
        agent_id=uuid4(),
        artifact_sha256="a" * 64,
        screened_image_sha256=hashlib.sha256(archive).hexdigest(),
        screened_image_url="https://artifact.example/image.tar",
        screened_image_id=image_id,
        screened_image_size_bytes=len(archive),
        bench_version=13,
        seed="b" * 64,
        lease_token=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    config = SimpleNamespace(docker_host=None, docker_bin="docker")
    runtime = ConversationRuntime(config, launch, "provider-only-secret")
    if tamper == "archive_digest":
        launch.screened_image_sha256 = "c" * 64
    elif tamper == "image_identity":
        launch.screened_image_id = "sha256:" + "c" * 64
    calls = []

    async def docker(*args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "info":
            return '["name=rootless"]'
        if args[:2] == ("image", "load"):
            return f"Loaded image ID: {image_id}"
        if args[:2] == ("image", "inspect") and args[-1] in (runtime.image, image_id):
            return json.dumps([{"Id": image_id, "Config": {}}])
        if args[0] == "inspect":
            return json.dumps({runtime.network: {"IPAddress": "172.20.0.2"}})
        if args[0] == "port":
            return "127.0.0.1:18080"
        if args[0] == "exec":
            return json.dumps({"status": 200, "body": "e30="})
        return ""

    runtime.docker = docker
    monkeypatch.setenv("SCREENER_GATEWAY_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "ditto_screener.conversation_runtime._write_openrouter_shim_certs",
        lambda _path: None,
    )
    real_client = httpx.AsyncClient

    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(
            200, content=archive if request.url.host == "artifact.example" else b"ok"
        )

    monkeypatch.setattr(
        "ditto_screener.conversation_runtime.httpx.AsyncClient",
        lambda **kwargs: real_client(
            **{"transport": httpx.MockTransport(handler), **kwargs}
        ),
    )
    if tamper:
        with pytest.raises(AssessmentFailure, match="mismatch"):
            await runtime.start()
        assert not any(
            args[0] in {"run", "create", "tag", "network"} for args, _ in calls
        )
        await runtime.stop()
        return
    assert await runtime.start() == "http://127.0.0.1"
    miner = next(args for args, _ in calls if args[:2] == ("run", "--detach"))
    assert "--read-only" in miner and "--cap-drop" in miner
    assert miner[miner.index("--network") + 1] == runtime.network
    assert "--privileged" not in miner and "--network=host" not in miner
    assert "--publish" not in miner
    assert miner[miner.index("--network-alias") + 1] == "agent"
    relay = next(args for args, _ in calls if args[0] == "create")
    assert "--publish" not in relay
    assert any(
        args[:3] == ("exec", "--interactive", runtime.relay) for args, _ in calls
    )
    assert "provider-only-secret" not in str(calls)
    assert not any(
        "leaf.key" in arg or "relay.py" in arg or "usage.json" in arg for arg in miner
    )
    assert any(args[:3] == ("network", "create", "--internal") for args, _ in calls)
    await runtime.stop()
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_rootful_executor_rejected_before_image_or_network_operations():
    launch = SimpleNamespace(assessment_id=uuid4())
    runtime = ConversationRuntime(SimpleNamespace(), launch, "secret")
    calls = []

    async def docker(*args, **_kwargs):
        calls.append(args)
        return '["name=seccomp"]'

    runtime.docker = docker
    with pytest.raises(AssessmentFailure, match="requires_rootless"):
        await runtime.preflight()
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("containerd", [False, True])
async def test_loaded_handle_is_bound_to_config_and_layers(tmp_path, containerd):
    archive, image_id = fixture_image(b"pinned filesystem bytes")
    portable = tmp_path / "portable.tar"
    portable.write_bytes(archive)
    handle = "sha256:" + "d" * 64 if containerd else image_id
    runtime = ConversationRuntime(
        SimpleNamespace(), SimpleNamespace(assessment_id=uuid4()), "secret"
    )
    calls = []

    async def docker(*args, **_kwargs):
        calls.append(args)
        if args[:2] == ("image", "load"):
            return f"Loaded image ID: {handle}"
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Id": handle, "Config": {}}])
        if args[:2] == ("image", "save"):
            from pathlib import Path

            Path(args[3]).write_bytes(archive)
        return ""

    runtime.docker = docker
    await runtime.load_verified_archive(portable, image_id)
    assert ("tag", handle, runtime.image) in calls
    assert any(args[:2] == ("image", "save") for args in calls) == containerd
    assert sorted(path.name for path in tmp_path.iterdir()) == ["portable.tar"]


@pytest.mark.asyncio
async def test_manifest_handle_with_tampered_layer_never_gets_tagged(tmp_path):
    archive, image_id = fixture_image(b"pinned filesystem bytes")
    tampered, unchanged_id = fixture_image(b"pinned filesystem bytes", tampered=True)
    assert unchanged_id == image_id
    portable = tmp_path / "portable.tar"
    portable.write_bytes(archive)
    handle = "sha256:" + "d" * 64
    runtime = ConversationRuntime(
        SimpleNamespace(), SimpleNamespace(assessment_id=uuid4()), "secret"
    )
    calls = []

    async def docker(*args, **_kwargs):
        calls.append(args)
        if args[:2] == ("image", "load"):
            return f"Loaded image ID: {handle}"
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Id": handle, "Config": {}}])
        if args[:2] == ("image", "save"):
            from pathlib import Path

            Path(args[3]).write_bytes(tampered)
        return ""

    runtime.docker = docker
    from ditto_screener.gate import _ScreenedImageExportError

    with pytest.raises(_ScreenedImageExportError, match="layer bytes"):
        await runtime.load_verified_archive(portable, image_id)
    assert not any(args[0] == "tag" for args in calls)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["portable.tar"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["config", "volumes", "tag_drift"])
async def test_manifest_handle_cannot_substitute_unverified_runtime(tmp_path, failure):
    archive, image_id = fixture_image()
    portable = tmp_path / "portable.tar"
    portable.write_bytes(archive)
    handle = "sha256:" + "d" * 64
    runtime = ConversationRuntime(
        SimpleNamespace(), SimpleNamespace(assessment_id=uuid4()), "secret"
    )
    calls = []

    async def docker(*args, **_kwargs):
        calls.append(args)
        if args[:2] == ("image", "load"):
            return f"Loaded image ID: {handle}"
        if args[:2] == ("image", "inspect"):
            identity = (
                image_id
                if failure == "tag_drift" and args[-1] == runtime.image
                else handle
            )
            config = {"Volumes": {"/data": {}}} if failure == "volumes" else {}
            return json.dumps([{"Id": identity, "Config": config}])
        if args[:2] == ("image", "save"):
            from pathlib import Path

            Path(args[3]).write_bytes(archive)
        return ""

    runtime.docker = docker
    expected = "sha256:" + "e" * 64 if failure == "config" else image_id
    with pytest.raises(AssessmentFailure, match="screened_image_runtime_mismatch"):
        await runtime.load_verified_archive(portable, expected)
    if failure != "tag_drift":
        assert not any(args[0] == "tag" for args in calls)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["portable.tar"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "loaded",
    [
        "",
        "Loaded image: untrusted:tag",
        "Loaded image ID: sha256:bad",
        "Loaded image ID: sha256:" + "a" * 64 + "\nLoaded image ID: sha256:" + "b" * 64,
    ],
)
async def test_ambiguous_load_identity_never_reaches_launch(loaded, tmp_path):
    runtime = ConversationRuntime(
        SimpleNamespace(), SimpleNamespace(assessment_id=uuid4()), "secret"
    )
    calls = []

    async def docker(*args, **_kwargs):
        calls.append(args)
        return loaded

    runtime.docker = docker
    with pytest.raises(
        AssessmentFailure, match="screened_image_load_identity_unavailable"
    ):
        await runtime.load_verified_archive(
            tmp_path / "portable.tar", "sha256:" + "c" * 64
        )
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["run", "seed", "health"])
@pytest.mark.parametrize(
    "error",
    [
        "timeout",
        "connection_failed",
        "response_too_large",
        "transport_failed",
        "exec_failed",
    ],
)
async def test_bridge_preserves_safe_operation_failure_without_replay(path, error):
    runtime = ConversationRuntime(
        SimpleNamespace(), SimpleNamespace(assessment_id=uuid4()), "secret"
    )
    calls = []

    async def docker(*args, **kwargs):
        calls.append((args, kwargs))
        assert json.loads(kwargs["input_data"])["timeout"] == 120
        assert kwargs["timeout"] == 125
        if error == "exec_failed":
            raise AssessmentFailure("sandbox_operation_failed")
        return json.dumps({"error": error})

    runtime.docker = docker
    async with httpx.AsyncClient(transport=runtime.transport()) as client:
        with pytest.raises(AssessmentFailure, match=f"^harness_{path}_{error}$"):
            await client.request(
                "GET" if path == "health" else "POST", f"http://127.0.0.1/{path}"
            )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_bridge_rejects_untrusted_error_text():
    runtime = ConversationRuntime(
        SimpleNamespace(), SimpleNamespace(assessment_id=uuid4()), "secret"
    )

    async def docker(*_args, **_kwargs):
        return '{"error":"secret provider URL or raw miner response"}'

    runtime.docker = docker
    async with httpx.AsyncClient(transport=runtime.transport()) as client:
        with pytest.raises(
            AssessmentFailure, match="^invalid_harness_transport_response$"
        ):
            await client.post("http://127.0.0.1/run")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["seed", "run"])
async def test_outer_operation_deadline_keeps_stage_and_does_not_retry(operation):
    import asyncio

    from ditto_screener.conversation import Limits, MemoryHarness
    from ditto_screening_protocol.conversation_story import story

    calls = []

    async def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/" + operation:
            await asyncio.sleep(1)
        return httpx.Response(200, json={"pairs": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        harness = MemoryHarness(
            client, "http://127.0.0.1", Limits(operation_seconds=0.01)
        )
        with pytest.raises(AssessmentFailure, match=f"^harness_{operation}_timeout$"):
            await harness.converse(story("a" * 64)[0])
    assert calls.count("/" + operation) == 1
    assert harness.exchanges == []
