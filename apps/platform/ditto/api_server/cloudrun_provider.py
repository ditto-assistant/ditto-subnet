"""Cloud Run adapter: Jobs for Kaniko and L1, internal Service for smoke."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ditto.api_server.cloudrun_client import AsyncCloudRunClient, CloudRunAPIError
from ditto.api_server.config import CloudRunScreeningConfig, TargonRentalConfig
from ditto.api_server.screening_provider import (
    BuildSpec,
    ProvisionObservation,
    ReviewSpec,
    ScreeningProviderError,
    SmokeSpec,
)

_JOB_PREFIX = "job:"
_SERVICE_PREFIX = "service:"
_SMOKE_GATEWAY_IMAGE = "python:3.12-alpine"
_SMOKE_GATEWAY_PORT = 11434
_SMOKE_GATEWAY_STUB = """
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
TAGS = {
  "models": [{
    "name": "embeddinggemma:latest",
    "model": "embeddinggemma:latest",
    "modified_at": "2024-01-01T00:00:00Z",
    "size": 1,
    "digest": "sha256:" + ("a" * 64),
    "details": {
      "format": "gguf",
      "family": "embeddinggemma",
      "families": ["embeddinggemma"],
      "parameter_size": "308M",
      "quantization_level": "F32",
    },
  }]
}
class H(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path.startswith("/api/tags") or self.path.startswith("/api/ps"):
            self._send(TAGS)
        else:
            self._send({"status": "ok"})
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if "show" in self.path:
            self._send({
                "modelfile": "FROM embeddinggemma",
                "details": TAGS["models"][0]["details"],
                "model_info": {"general.architecture": "embeddinggemma"},
            })
        elif "chat/completions" in self.path:
            self._send({
                "id": "chatcmpl-smoke",
                "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
            })
        else:
            vec = [0.0] * 768
            vec[0] = 1.0
            self._send({
                "model": "embeddinggemma:latest",
                "embedding": vec,
                "embeddings": [vec],
                "data": [{"index": 0, "embedding": vec}],
            })
    def log_message(self, *args):
        pass
HTTPServer(("0.0.0.0", 11434), H).serve_forever()
"""
_SMOKE_LOCAL_GATEWAY = f"http://127.0.0.1:{_SMOKE_GATEWAY_PORT}"
_SMOKE_EXTRA_ENV = (
    ("OLLAMA_BASE_URL", _SMOKE_LOCAL_GATEWAY),
    ("DITTOBENCH_INFERENCE_BASE_URL", _SMOKE_LOCAL_GATEWAY),
    ("OPENAI_BASE_URL", f"{_SMOKE_LOCAL_GATEWAY}/v1"),
    ("OPENAI_API_KEY", "relay"),
    ("CHUTES_BASE_URL", f"{_SMOKE_LOCAL_GATEWAY}/v1"),
    ("CHUTES_API_KEY", "relay"),
    ("DITTOBENCH_PROVIDER", "chutes"),
)


class CloudRunComputeProvider:
    name = "cloudrun"
    stored_provider = "gcp"

    def __init__(
        self,
        client: AsyncCloudRunClient,
        config: CloudRunScreeningConfig,
        targon_config: TargonRentalConfig,
    ) -> None:
        self._client = client
        self._config = config
        self._targon_config = targon_config

    async def capacity_ok(self) -> bool:
        return self._config.enabled

    async def create_build(self, spec: BuildSpec) -> str:
        try:
            await self._client.create_job(
                spec.name,
                image=spec.image,
                env=spec.env,
                service_account=self._config.untrusted_sa_email,
                timeout_seconds=int(self._targon_config.build_timeout_seconds),
                cpu="4",
                memory="16Gi",
            )
        except CloudRunAPIError as error:
            raise ScreeningProviderError(
                provider=self.name,
                operation="create_job",
                reason=type(error).__name__,
            ) from error
        return f"{_JOB_PREFIX}{spec.name}"

    async def create_smoke(self, spec: SmokeSpec) -> str:
        env = spec.env + _SMOKE_EXTRA_ENV
        sidecar = {
            "name": "gateway",
            "image": _SMOKE_GATEWAY_IMAGE,
            "command": ["python", "-c", _SMOKE_GATEWAY_STUB],
            "resources": {"limits": {"cpu": "0.25", "memory": "256Mi"}},
            "startupProbe": {
                "tcpSocket": {"port": _SMOKE_GATEWAY_PORT},
                "periodSeconds": 1,
                "failureThreshold": 30,
            },
        }
        try:
            await self._client.create_service(
                spec.name,
                image=spec.image,
                env=env,
                service_account=self._config.untrusted_sa_email,
                invoker_sa_email=self._config.platform_invoker_sa_email,
                timeout_seconds=int(self._targon_config.runtime_timeout_seconds),
                sidecar=sidecar,
            )
        except CloudRunAPIError as error:
            raise ScreeningProviderError(
                provider=self.name,
                operation="create_service",
                reason=type(error).__name__,
            ) from error
        return f"{_SERVICE_PREFIX}{spec.name}"

    async def create_source_review(self, spec: ReviewSpec) -> str:
        try:
            await self._client.create_job(
                spec.name,
                image=spec.image,
                env=spec.env,
                service_account=self._config.untrusted_sa_email,
                timeout_seconds=int(self._targon_config.source_review_timeout_seconds)
                + 900,
                cpu="2",
                memory="4Gi",
                commands=spec.commands,
                args=spec.args,
            )
        except CloudRunAPIError as error:
            raise ScreeningProviderError(
                provider=self.name,
                operation="create_job",
                reason=type(error).__name__,
            ) from error
        return f"{_JOB_PREFIX}{spec.name}"

    async def start(self, resource_id: str) -> None:
        kind, name = _split(resource_id)
        if kind != "job":
            return
        try:
            await self._client.run_job(name)
        except CloudRunAPIError as error:
            raise ScreeningProviderError(
                provider=self.name,
                operation="run_job",
                reason=type(error).__name__,
            ) from error

    async def provision_status(self, resource_id: str) -> str:
        return (await self.observe_provision(resource_id)).status

    async def observe_provision(self, resource_id: str) -> ProvisionObservation:
        kind, name = _split(resource_id)
        try:
            if kind == "job":
                status = await self._job_status(name)
            else:
                status = await self._service_status(name)
        except CloudRunAPIError:
            return ProvisionObservation(status="")
        return ProvisionObservation(status=status)

    async def wait_until_running(self, resource_id: str, timeout_seconds: float) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            status = await self.provision_status(resource_id)
            if status == "running":
                return "running"
            if status in {"error", "deleted"}:
                return "error"
            remaining = deadline - loop.time()
            if remaining <= 0:
                return "timeout"
            await asyncio.sleep(min(1.0, remaining))

    async def probe_smoke(self, resource_id: str, *, timeout_seconds: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        kind, name = _split(resource_id)
        if kind != "service":
            return False
        while loop.time() < deadline:
            status = await self.provision_status(resource_id)
            if status == "error":
                return False
            if status == "running":
                try:
                    service = await self._client.get_service(name)
                except CloudRunAPIError:
                    await asyncio.sleep(1)
                    continue
                uri = str(service.get("uri", "")).rstrip("/")
                if uri.startswith("https://") and await self._healthy(uri):
                    return True
            await asyncio.sleep(1)
        return False

    async def delete(self, resource_id: str) -> bool:
        kind, name = _split(resource_id)
        try:
            if kind == "job":
                await self._client.delete_job(name)
            else:
                await self._client.delete_service(name)
        except CloudRunAPIError:
            return False
        return True

    async def _healthy(self, uri: str) -> bool:
        try:
            token = await self._client.identity_token(uri)
        except CloudRunAPIError:
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{uri}/health",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError:
            return False
        return 200 <= response.status_code < 300

    async def _job_status(self, job_id: str) -> str:
        job = await self._client.get_job(job_id)
        execution = _job_execution_ref(job)
        if execution is None:
            return "pending"
        completion = str(execution.get("completionStatus", "")).upper()
        if completion in {"EXECUTION_FAILED", "EXECUTION_CANCELLED"}:
            return "error"
        if completion in {"EXECUTION_RUNNING", "EXECUTION_SUCCEEDED"}:
            return "running"
        name = str(execution.get("name", ""))
        if not name:
            return "pending"
        detail = await self._client.get_execution(name)
        counts = _execution_status(detail)
        if int(counts.get("failedCount", 0) or 0) > 0:
            return "error"
        if int(counts.get("cancelledCount", 0) or 0) > 0:
            return "error"
        if int(counts.get("runningCount", 0) or 0) > 0:
            return "running"
        if int(counts.get("succeededCount", 0) or 0) > 0:
            return "running"
        if counts.get("completionTime") or detail.get("completionTime"):
            return "running"
        return "pending"

    async def _service_status(self, service_id: str) -> str:
        service = await self._client.get_service(service_id)
        terminal = service.get("terminalCondition")
        if isinstance(terminal, dict):
            state = str(terminal.get("state", "")).upper()
            if state == "CONDITION_SUCCEEDED":
                return "running"
            if state == "CONDITION_FAILED":
                return "error"
        conditions = service.get("conditions")
        if isinstance(conditions, list):
            for row in conditions:
                if not isinstance(row, dict):
                    continue
                if str(row.get("type", "")) == "Ready" and str(
                    row.get("state", "")
                ) == ("CONDITION_SUCCEEDED"):
                    return "running"
        return "pending"


def _execution_status(detail: dict[str, Any]) -> dict[str, Any]:
    """Cloud Run v2 nests counts under ``status``; tolerate flattened doubles."""
    nested = detail.get("status")
    if isinstance(nested, dict):
        return nested
    return detail


def _job_execution_ref(job: dict[str, Any]) -> dict[str, Any] | None:
    """Return the latest execution reference from a v2 Job body.

    Production JSON nests ``latestCreatedExecution`` under ``status``. A
    top-level copy is only a test double.
    """
    nested = _execution_status(job).get("latestCreatedExecution")
    if isinstance(nested, dict):
        return nested
    top = job.get("latestCreatedExecution")
    if isinstance(top, dict):
        return top
    return None


def _split(resource_id: str) -> tuple[str, str]:
    if resource_id.startswith(_JOB_PREFIX):
        return "job", resource_id[len(_JOB_PREFIX) :]
    if resource_id.startswith(_SERVICE_PREFIX):
        return "service", resource_id[len(_SERVICE_PREFIX) :]
    return "job", resource_id
