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


def fixture_image():
    config = json.dumps({"rootfs": {"type": "layers", "diff_ids": []}}).encode()
    digest = "sha256:" + hashlib.sha256(config).hexdigest()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in (
            ("config.json", config),
            (
                "manifest.json",
                json.dumps(
                    [{"Config": "config.json", "RepoTags": None, "Layers": []}]
                ).encode(),
            ),
        ):
            info = tarfile.TarInfo(name)
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
        if args[:2] == ("image", "inspect") and args[-1] == runtime.image:
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
