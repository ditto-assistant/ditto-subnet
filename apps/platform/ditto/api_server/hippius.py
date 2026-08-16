"""Hippius S3-compatible client for miner profile pictures.

Hippius rejects SDK-direct PutObject/GetObject with SignatureDoesNotMatch
even when the same credentials can presign. Uploads and downloads go through
a presigned URL over plain HTTP, matching the ditto-assistant backend.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from ditto.api_server.storage.errors import (
    ObjectDownloadFailedError,
    ObjectNotFoundError,
    ObjectUploadFailedError,
    StorageConfigurationError,
)

if TYPE_CHECKING:
    from types import TracebackType

DEFAULT_ENDPOINT = "https://s3.hippius.com"
DEFAULT_REGION = "decentralized"
_PRESIGN_TTL_SECONDS = 300


@dataclass(frozen=True)
class HippiusConfig:
    endpoint_url: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = DEFAULT_REGION
    public_prefix: str | None = None


def parse_hippius_config_from_env() -> HippiusConfig | None:
    """Return Hippius config when the feature is fully configured.

    Missing bucket or keys disables avatars without failing Platform boot.
    """
    bucket = os.environ.get("HIPPIUS_BUCKET", "").strip()
    access_key = os.environ.get("HIPPIUS_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("HIPPIUS_SECRET_ACCESS_KEY", "").strip()
    if not bucket or not access_key or not secret_key:
        return None
    if not access_key.startswith("hip_"):
        raise StorageConfigurationError(
            "HIPPIUS_ACCESS_KEY_ID must start with hip_"
        )
    return HippiusConfig(
        endpoint_url=os.environ.get("HIPPIUS_ENDPOINT", DEFAULT_ENDPOINT).strip()
        or DEFAULT_ENDPOINT,
        bucket=bucket,
        access_key=access_key,
        secret_key=secret_key,
        region=os.environ.get("HIPPIUS_REGION", DEFAULT_REGION).strip()
        or DEFAULT_REGION,
        public_prefix=os.environ.get("HIPPIUS_PUBLIC_PREFIX", "").strip() or None,
    )


class HippiusClient:
    """Presigned-PUT/GET client for one Hippius bucket."""

    def __init__(self, config: HippiusConfig) -> None:
        import aioboto3
        from botocore.config import Config

        self._config = config
        self._session = aioboto3.Session(
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            region_name=config.region,
        )
        self._client_config = Config(
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            signature_version="s3v4",
        )
        self._http = httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> HippiusClient:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def public_url(self, key: str) -> str:
        if self._config.public_prefix:
            return f"{self._config.public_prefix.rstrip('/')}/{key}"
        return (
            f"{self._config.endpoint_url.rstrip('/')}/"
            f"{self._config.bucket.strip('/')}/{key}"
        )

    async def put_object(self, *, key: str, body: bytes, content_type: str) -> str:
        url = await self._presign("put_object", key, content_type=content_type)
        try:
            response = await self._http.put(
                url, content=body, headers={"Content-Type": content_type}
            )
        except httpx.HTTPError as exc:
            raise ObjectUploadFailedError(
                f"hippius put {key!r} failed: {exc}"
            ) from exc
        if response.status_code >= 300:
            raise ObjectUploadFailedError(
                f"hippius put {key!r}: http {response.status_code}"
            )
        return self.public_url(key)

    async def get_object(self, *, key: str) -> bytes:
        url = await self._presign("get_object", key)
        try:
            response = await self._http.get(url)
        except httpx.HTTPError as exc:
            raise ObjectDownloadFailedError(
                f"hippius get {key!r} failed: {exc}"
            ) from exc
        if response.status_code == 404:
            raise ObjectNotFoundError(f"hippius object {key!r} not found")
        if response.status_code >= 300:
            raise ObjectDownloadFailedError(
                f"hippius get {key!r}: http {response.status_code}"
            )
        return response.content

    async def delete_object(self, *, key: str) -> None:
        url = await self._presign("delete_object", key)
        try:
            response = await self._http.request("DELETE", url)
        except httpx.HTTPError as exc:
            raise ObjectUploadFailedError(
                f"hippius delete {key!r} failed: {exc}"
            ) from exc
        if response.status_code not in {200, 204, 404}:
            raise ObjectUploadFailedError(
                f"hippius delete {key!r}: http {response.status_code}"
            )

    async def _presign(
        self, operation: str, key: str, *, content_type: str | None = None
    ) -> str:
        params: dict[str, str] = {
            "Bucket": self._config.bucket,
            "Key": key,
        }
        if content_type is not None and operation == "put_object":
            params["ContentType"] = content_type
        async with self._session.client(
            "s3",
            endpoint_url=self._config.endpoint_url,
            use_ssl=self._config.endpoint_url.startswith("https"),
            config=self._client_config,
        ) as s3:
            return await s3.generate_presigned_url(
                operation, Params=params, ExpiresIn=_PRESIGN_TTL_SECONDS
            )


def create_hippius_client(
    config: HippiusConfig | None = None,
) -> HippiusClient | None:
    resolved = config if config is not None else parse_hippius_config_from_env()
    if resolved is None:
        return None
    return HippiusClient(resolved)
