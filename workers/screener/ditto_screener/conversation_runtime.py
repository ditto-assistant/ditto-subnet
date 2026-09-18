"""Rootless, fresh-image runtime for the shadow conversation instrument."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from ditto_screener.conversation import AssessmentFailure
from ditto_screener.gate import (
    _CANARY_IMAGE,
    BuildGate,
    _prepare_gateway_state,
    _write_openrouter_shim_certs,
)
from ditto_screening_protocol.conversation import ConversationLaunch, HarnessUsage

if TYPE_CHECKING:
    from ditto_screener.config import ScreenerConfig


class ConversationRuntime:
    def __init__(
        self, config: ScreenerConfig, launch: ConversationLaunch, provider_key: str
    ):
        self.config, self.launch, self.provider_key = config, launch, provider_key
        suffix = launch.assessment_id.hex
        self.network = "ditto-conversation-" + suffix
        self.relay = self.network + "-relay"
        self.container = self.network + "-agent"
        self.image = "ditto-conversation:" + suffix
        self.state: Path | None = None

    async def docker(
        self, *args: str, timeout: float = 60, provider: bool = False
    ) -> str:
        env = dict(os.environ)
        if self.config.docker_host:
            env["DOCKER_HOST"] = self.config.docker_host
        if provider:
            env["OPENROUTER_API_KEY"] = self.provider_key
        process = await asyncio.create_subprocess_exec(
            self.config.docker_bin,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            async with asyncio.timeout(timeout):
                # CLI output is never miner stdout (logs are deliberately unused).
                out, _ = await process.communicate()
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise AssessmentFailure("sandbox_operation_failed")
        return out.decode().strip()

    async def preflight(self) -> None:
        security = json.loads(
            await self.docker("info", "--format", "{{json .SecurityOptions}}")
        )
        if not isinstance(security, list) or not any(
            isinstance(item, str) and "rootless" in item for item in security
        ):
            raise AssessmentFailure("sandbox_requires_rootless")
        # Pull only this release-pinned trusted helper, before claiming paid work.
        await self.docker("image", "inspect", _CANARY_IMAGE)

    async def start(self) -> str:
        await self.preflight()
        directory, _ = _prepare_gateway_state()
        self.state = Path(directory)
        archive = self.state / "image.tar"
        portable = self.state / "portable.tar"
        parsed = urlsplit(self.launch.screened_image_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise AssessmentFailure("invalid_screened_image_url")
        digest = hashlib.sha256()
        size = 0
        async with (
            httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=300
            ) as client,
            client.stream("GET", self.launch.screened_image_url) as response,
        ):
            if response.status_code != 200:
                raise AssessmentFailure("screened_image_download_failed")
            with archive.open("xb") as handle:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.launch.screened_image_size_bytes:
                        raise AssessmentFailure("screened_image_size_mismatch")
                    digest.update(chunk)
                    handle.write(chunk)
        if (
            size != self.launch.screened_image_size_bytes
            or digest.hexdigest() != self.launch.screened_image_sha256
        ):
            raise AssessmentFailure("screened_image_digest_mismatch")
        verified = await asyncio.to_thread(
            BuildGate._portable_image_archive,
            str(archive),
            str(portable),
            deadline=time.monotonic() + 300,
        )
        if verified.image_id != self.launch.screened_image_id:
            raise AssessmentFailure("screened_image_identity_mismatch")
        await self.docker("image", "load", "--input", str(portable), timeout=300)
        await self.docker("tag", verified.image_id, self.image)
        info = json.loads(await self.docker("image", "inspect", self.image))[0]
        if info["Id"] != self.launch.screened_image_id or info.get("Config", {}).get(
            "Volumes"
        ):
            raise AssessmentFailure("screened_image_runtime_mismatch")
        archive.unlink()
        portable.unlink()
        await asyncio.to_thread(_write_openrouter_shim_certs, directory)
        script = self.state / "relay.py"
        shutil.copyfile(Path(__file__).with_name("conversation_relay.py"), script)
        script.chmod(0o444)
        usage = self.state / "usage.json"
        usage.touch(mode=0o600)
        usage.chmod(0o666)  # Host-owned file mounted only into the trusted sidecar.
        await self.docker("network", "create", "--internal", self.network)
        common = [
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--log-driver",
            "none",
            "--ipc",
            "none",
        ]
        await self.docker(
            "create",
            "--name",
            self.relay,
            "--network",
            "bridge",
            *common,
            "--cap-add",
            "NET_BIND_SERVICE",
            "--memory",
            "256m",
            "--pids-limit",
            "64",
            "--cpus",
            "1",
            "--env",
            "OPENROUTER_API_KEY",
            "--mount",
            f"type=bind,src={script},dst=/relay.py,readonly",
            "--mount",
            f"type=bind,src={usage},dst=/state/usage.json",
            "--mount",
            f"type=bind,src={self.state / 'leaf.crt'},dst=/private/leaf.crt,readonly",
            "--mount",
            f"type=bind,src={self.state / 'leaf.key'},dst=/private/leaf.key,readonly",
            _CANARY_IMAGE,
            "python",
            "/relay.py",
            provider=True,
        )
        await self.docker(
            "network",
            "connect",
            "--alias",
            "gateway",
            "--alias",
            "host.docker.internal",
            self.network,
            self.relay,
        )
        await self.docker("start", self.relay)
        networks = json.loads(
            await self.docker(
                "inspect", "--format", "{{json .NetworkSettings.Networks}}", self.relay
            )
        )
        relay_ip = networks[self.network]["IPAddress"]
        gateway = "http://gateway:11434"
        environment = {
            "DITTOBENCH_PROVIDER": "platform",
            "DITTOBENCH_MODEL": "openai/gpt-oss-20b",
            "DITTOBENCH_DB": "/tmp/dittobench.db",
            "HOME": "/tmp",
            "DITTOBENCH_INFERENCE_BASE_URL": gateway + "/v1",
            "OPENAI_BASE_URL": gateway + "/v1",
            "OPENAI_API_BASE": gateway + "/v1",
            "OPENROUTER_BASE_URL": gateway + "/v1",
            "CHUTES_BASE_URL": gateway + "/v1",
            "OLLAMA_BASE_URL": gateway,
            "OPENAI_API_KEY": "sandbox",
            "OPENROUTER_API_KEY": "sandbox",
            "CHUTES_API_KEY": "sandbox",
            "SSL_CERT_FILE": "/run/ca.pem",
            "REQUESTS_CA_BUNDLE": "/run/ca.pem",
            "CURL_CA_BUNDLE": "/run/ca.pem",
            "NODE_EXTRA_CA_CERTS": "/run/ca.pem",
        }
        args = [
            "run",
            "--detach",
            "--name",
            self.container,
            "--network",
            self.network,
            *common,
            "--user",
            "65532:65532",
            "--memory",
            "4g",
            "--memory-swap",
            "4g",
            "--cpus",
            "2",
            "--pids-limit",
            "256",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=1g",
            "--publish",
            "127.0.0.1::8080",
            "--add-host",
            f"openrouter.ai:{relay_ip}",
            "--mount",
            f"type=bind,src={self.state / 'ca-bundle.pem'},dst=/run/ca.pem,readonly",
        ]
        for key, value in environment.items():
            args.extend(("--env", f"{key}={value}"))
        await self.docker(*args, self.image)
        binding = await self.docker("port", self.container, "8080/tcp")
        if (
            not binding.startswith("127.0.0.1:")
            or not binding.removeprefix("127.0.0.1:").isdigit()
        ):
            raise AssessmentFailure("sandbox_port_invalid")
        url = "http://" + binding
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            for _ in range(60):
                try:
                    if (await client.get(url + "/health")).is_success:
                        return url
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
        raise AssessmentFailure("sandbox_health_timeout")

    async def stop(self) -> HarnessUsage | None:
        # Stop the untrusted caller first, then settle any in-flight provider
        # dispatch. Missing/partial usage is uncertainty, never a zero-cost claim.
        with contextlib.suppress(Exception):
            await self.docker("rm", "--force", self.container)
        with contextlib.suppress(Exception):
            await self.docker("stop", "--time", "120", self.relay, timeout=130)
        with contextlib.suppress(Exception):
            await self.docker("rm", "--force", self.relay)
        usage = None
        if self.state:
            with contextlib.suppress(OSError, ValueError):
                usage = HarnessUsage.model_validate_json(
                    (self.state / "usage.json").read_text()
                )
        with contextlib.suppress(Exception):
            await self.docker("network", "rm", self.network)
        with contextlib.suppress(Exception):
            await self.docker("image", "rm", self.image)
        if self.state:
            shutil.rmtree(self.state, ignore_errors=True)
        return usage
