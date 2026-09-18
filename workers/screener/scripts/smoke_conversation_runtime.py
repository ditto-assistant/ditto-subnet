#!/usr/bin/env python3
"""Smoke the released launcher with a disposable synthetic image; no inference."""

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx

from ditto_screener.conversation import Limits, MemoryHarness
from ditto_screener.conversation_runtime import ConversationRuntime
from ditto_screener.gate import _CANARY_IMAGE
from ditto_screening_protocol.conversation import ConversationLaunch
from ditto_screening_protocol.conversation_story import story

fixture = "ditto-conversation-fixture:" + uuid4().hex


def docker(*args):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=True, timeout=120
    ).stdout.strip()


SERVER = """import json, ssl, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def respond(self, body):
        data=json.dumps(body).encode();self.send_response(200)
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_GET(self): self.respond({'status':'ok'})
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/seed': self.respond({'pairs':len(body['pairs'])}); return
        result=json.load(urllib.request.urlopen('https://openrouter.ai/api/v1/models',timeout=5))
        self.respond({'final_text':'TLS relay verified: '+result['data'][0]['id']})
HTTPServer(('0.0.0.0',8080),Handler).serve_forever()
"""


async def main(directory):
    root = Path(directory)
    (root / "fixture.py").write_text(SERVER)
    (root / "fixture.py").chmod(0o444)
    (root / "Dockerfile").write_text(
        "FROM "
        + _CANARY_IMAGE
        + '\nCOPY fixture.py /fixture.py\nCMD ["python","/fixture.py"]\n'
    )
    docker("build", "--network", "none", "--tag", fixture, directory)
    image_id = json.loads(docker("image", "inspect", fixture))[0]["Id"]
    archive = root / "image.tar"
    docker("image", "save", "--output", str(archive), fixture)
    data = archive.read_bytes()
    launch = ConversationLaunch(
        assessment_id=uuid4(),
        agent_id=uuid4(),
        artifact_sha256="a" * 64,
        screened_image_sha256=hashlib.sha256(data).hexdigest(),
        screened_image_size_bytes=len(data),
        screened_image_id=image_id,
        screened_image_url="https://fixture.invalid/image.tar",
        bench_version=12,
        seed="b" * 64,
        lease_token=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    real_client = httpx.AsyncClient

    class Transport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.real = httpx.AsyncHTTPTransport()

        async def handle_async_request(self, request):
            if request.url.host == "fixture.invalid":
                return httpx.Response(200, content=data)
            return await self.real.handle_async_request(request)

        async def aclose(self):
            await self.real.aclose()

    import ditto_screener.conversation_runtime as module

    module.httpx.AsyncClient = lambda **kwargs: real_client(
        **{"transport": Transport(), **kwargs}
    )
    runtime = ConversationRuntime(
        SimpleNamespace(docker_bin="docker", docker_host=os.environ.get("DOCKER_HOST")),
        launch,
        "preflight-placeholder",
    )
    usage = None
    try:
        url = await runtime.start()
        async with real_client(
            trust_env=False, timeout=20, transport=runtime.transport()
        ) as client:
            harness = MemoryHarness(client, url, Limits())
            for turn in story(launch.seed):
                result = await harness.converse(turn)
                assert result.assistant == "TLS relay verified: openai/gpt-oss-20b"
            assert len(harness.exchanges) == 30
    finally:
        module.httpx.AsyncClient = real_client
        usage = await runtime.stop()
    assert usage and usage.requests == 0 and not usage.failed and not usage.unmetered
    assert not runtime.state.exists()
    assert not docker("ps", "--all", "--quiet", "--filter", "name=" + runtime.network)
    print(
        json.dumps(
            {
                "launcher": "passed",
                "exchanges": 30,
                "sessions": 10,
                "tls_relay": True,
                "provider_requests": usage.requests,
                "cleanup": "passed",
                "runtime_sha256": hashlib.sha256(
                    Path(module.__file__).read_bytes()
                ).hexdigest(),
            }
        )
    )


try:
    security = json.loads(docker("info", "--format", "{{json .SecurityOptions}}"))
    assert any("rootless" in item for item in security), "a rootless daemon is required"
    with tempfile.TemporaryDirectory(prefix="conversation-preflight-") as directory:
        asyncio.run(main(directory))
finally:
    subprocess.run(["docker", "image", "rm", fixture], capture_output=True, timeout=60)
