"""Cloud Run v2 REST client for screening Jobs and internal smoke Services.

Untrusted containers receive only attempt-bound job tokens. Cloud, GitHub,
Platform, and provider credentials stay on the Platform VM.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

TokenGetter = Callable[[], Awaitable[str]]
IdentityGetter = Callable[[str], Awaitable[str]]


class CloudRunAPIError(RuntimeError):
    def __init__(self, *, operation: str, status: int | None, reason: str) -> None:
        self.operation = operation
        self.status = status
        self.reason = reason
        status_text = "transport" if status is None else str(status)
        super().__init__(f"Cloud Run {operation} failed ({status_text}): {reason}")


async def metadata_access_token() -> str:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(
            "http://metadata.google.internal/computeMetadata/v1/"
            "instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"},
        )
    if response.status_code >= 400:
        raise CloudRunAPIError(
            operation="metadata access token",
            status=response.status_code,
            reason="HTTP error",
        )
    token = str(response.json().get("access_token", "")).strip()
    if len(token) < 20:
        raise CloudRunAPIError(
            operation="metadata access token",
            status=None,
            reason="invalid token",
        )
    return token


async def metadata_identity_token(audience: str) -> str:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(
            "http://metadata.google.internal/computeMetadata/v1/"
            "instance/service-accounts/default/identity",
            params={"audience": audience, "format": "full"},
            headers={"Metadata-Flavor": "Google"},
        )
    if response.status_code >= 400:
        raise CloudRunAPIError(
            operation="metadata identity token",
            status=response.status_code,
            reason="HTTP error",
        )
    token = response.text.strip()
    if len(token) < 20:
        raise CloudRunAPIError(
            operation="metadata identity token",
            status=None,
            reason="invalid token",
        )
    return token


class AsyncCloudRunClient:
    def __init__(
        self,
        *,
        project: str,
        region: str,
        access_token: TokenGetter | None = None,
        identity_token: IdentityGetter | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._project = project
        self._region = region
        self._access_token = access_token or metadata_access_token
        self._identity_token = identity_token or metadata_identity_token
        self._client = httpx.AsyncClient(timeout=timeout_seconds)
        self._base = (
            f"https://run.googleapis.com/v2/projects/{quote(project, safe='')}"
            f"/locations/{quote(region, safe='')}"
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def job_name(self, job_id: str) -> str:
        return f"{self._base}/jobs/{job_id}"

    def service_name(self, service_id: str) -> str:
        return f"{self._base}/services/{service_id}"

    async def create_job(
        self,
        job_id: str,
        *,
        image: str,
        env: tuple[tuple[str, str], ...],
        service_account: str,
        timeout_seconds: int,
        cpu: str,
        memory: str,
        commands: tuple[str, ...] | None = None,
        args: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        container: dict[str, Any] = {
            "image": image,
            "env": [{"name": key, "value": value} for key, value in env],
            "resources": {"limits": {"cpu": cpu, "memory": memory}},
        }
        if commands:
            container["command"] = list(commands)
        if args:
            container["args"] = list(args)
        body = {
            "launchStage": "GA",
            "template": {
                "taskCount": 1,
                "parallelism": 1,
                "template": {
                    "containers": [container],
                    "timeout": f"{int(timeout_seconds)}s",
                    "serviceAccount": service_account,
                    "maxRetries": 0,
                },
            },
        }
        return await self._request(
            "POST",
            f"{self._base}/jobs",
            payload=body,
            params={"jobId": job_id},
        )

    async def run_job(self, job_id: str) -> dict[str, Any]:
        return await self._request("POST", f"{self.job_name(job_id)}:run")

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return await self._request("GET", self.job_name(job_id))

    async def get_execution(self, execution_name: str) -> dict[str, Any]:
        if execution_name.startswith("https://"):
            url = execution_name
        elif execution_name.startswith("projects/"):
            url = f"https://run.googleapis.com/v2/{execution_name}"
        else:
            url = execution_name
        return await self._request("GET", url)

    async def delete_job(self, job_id: str) -> None:
        await self._request("DELETE", self.job_name(job_id), allow_missing=True)

    async def create_service(
        self,
        service_id: str,
        *,
        image: str,
        env: tuple[tuple[str, str], ...],
        service_account: str,
        invoker_sa_email: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        body = {
            "ingress": "INGRESS_TRAFFIC_INTERNAL_ONLY",
            "template": {
                "serviceAccount": service_account,
                "timeout": f"{int(timeout_seconds)}s",
                "maxInstanceRequestConcurrency": 1,
                "scaling": {"minInstanceCount": 0, "maxInstanceCount": 1},
                "containers": [
                    {
                        "image": image,
                        "ports": [{"containerPort": 8080}],
                        "env": [{"name": key, "value": value} for key, value in env],
                        "resources": {"limits": {"cpu": "1", "memory": "2Gi"}},
                    }
                ],
            },
        }
        created = await self._request(
            "POST",
            f"{self._base}/services",
            payload=body,
            params={"serviceId": service_id},
        )
        await self._request(
            "POST",
            f"{self.service_name(service_id)}:setIamPolicy",
            payload={
                "policy": {
                    "bindings": [
                        {
                            "role": "roles/run.invoker",
                            "members": [f"serviceAccount:{invoker_sa_email}"],
                        }
                    ]
                }
            },
        )
        return created

    async def get_service(self, service_id: str) -> dict[str, Any]:
        return await self._request("GET", self.service_name(service_id))

    async def delete_service(self, service_id: str) -> None:
        await self._request("DELETE", self.service_name(service_id), allow_missing=True)

    async def identity_token(self, audience: str) -> str:
        return await self._identity_token(audience)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        allow_missing: bool = False,
    ) -> dict[str, Any]:
        token = await self._access_token()
        try:
            response = await self._client.request(
                method,
                url,
                json=payload,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as error:
            raise CloudRunAPIError(
                operation=f"{method} {url}",
                status=None,
                reason=type(error).__name__,
            ) from error
        if allow_missing and response.status_code == 404:
            return {}
        if response.status_code == 204 or not response.content:
            return {}
        if response.status_code >= 400:
            raise CloudRunAPIError(
                operation=f"{method} {url.split('?', 1)[0]}",
                status=response.status_code,
                reason="HTTP error",
            )
        try:
            value = response.json()
        except ValueError:
            raise CloudRunAPIError(
                operation=f"{method} {url}",
                status=response.status_code,
                reason="non-JSON response",
            ) from None
        if not isinstance(value, dict):
            raise CloudRunAPIError(
                operation=f"{method} {url}",
                status=response.status_code,
                reason="invalid response shape",
            )
        return value
