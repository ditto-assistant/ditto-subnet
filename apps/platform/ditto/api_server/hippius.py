"""Hippius S3-compatible client for miner profile pictures.

Hippius rejects SDK-direct PutObject/GetObject/HeadObject/DeleteObject
with SignatureDoesNotMatch even when the same credentials can presign
(same class of bug as ditto-assistant/backend#1078 / #1088). Uploads
and downloads go through a presigned URL over plain HTTP.

The avatar bucket stays private. Dashboard clients never hit Hippius
directly; they load ``/api/v1/public/miners/{hotkey}/avatar``.
"""

from __future__ import annotations

import asyncio
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
_RETRY_ATTEMPTS = 1
_RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class HippiusConfig:
    endpoint_url: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = DEFAULT_REGION
    public_prefix: str | None = None


def ensure_https(endpoint: str) -> str:
    value = endpoint.strip()
    if value.startswith("http://"):
        value = "https://" + value[len("http://") :]
    elif not value.startswith("https://"):
        value = "https://" + value
    return value.rstrip("/")


def normalize_object_key(key: str) -> str:
    parts = [
        part
        for part in key.strip().lstrip("/").split("/")
        if part and part not in {".", ".."}
    ]
    return "/".join(parts)


def parse_traces_hippius_config_from_env() -> HippiusConfig | None:
    """Hippius config for the PRIVATE inference-trace bucket.

    The trace capture (services/model-relay internal/traces) ships to its own
    bucket -- default ``ditto-subnet-traces`` -- under the same Hippius account
    as the avatars. Credentials/endpoint/region come from the
    ``INFERENCE_TRACE_SINK_HIPPIUS_*`` variables the relay reads, falling back
    to the ``HIPPIUS_*`` pair this API already holds, so the operator grants
    the key access to both buckets and configures nothing else. None disables
    the trace endpoints (503) without affecting boot.
    """

    def pick(primary: str, fallback: str, default: str = "") -> str:
        value = os.environ.get(primary, "").strip()
        if value:
            return value
        return os.environ.get(fallback, default).strip()

    access_key = pick(
        "INFERENCE_TRACE_SINK_HIPPIUS_ACCESS_KEY_ID", "HIPPIUS_ACCESS_KEY_ID"
    )
    secret_key = pick(
        "INFERENCE_TRACE_SINK_HIPPIUS_SECRET_ACCESS_KEY", "HIPPIUS_SECRET_ACCESS_KEY"
    )
    if not access_key or not secret_key:
        return None
    bucket = (
        os.environ.get("INFERENCE_TRACE_SINK_HIPPIUS_BUCKET", "").strip()
        or "ditto-subnet-traces"
    )
    return HippiusConfig(
        endpoint_url=ensure_https(
            pick(
                "INFERENCE_TRACE_SINK_HIPPIUS_ENDPOINT",
                "HIPPIUS_ENDPOINT",
                DEFAULT_ENDPOINT,
            )
        ),
        bucket=bucket,
        access_key=access_key,
        secret_key=secret_key,
        region=pick(
            "INFERENCE_TRACE_SINK_HIPPIUS_REGION", "HIPPIUS_REGION", DEFAULT_REGION
        )
        or DEFAULT_REGION,
    )


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
        raise StorageConfigurationError("HIPPIUS_ACCESS_KEY_ID must start with hip_")
    return HippiusConfig(
        endpoint_url=ensure_https(
            os.environ.get("HIPPIUS_ENDPOINT", DEFAULT_ENDPOINT).strip()
            or DEFAULT_ENDPOINT
        ),
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
            response_checksum_validation="when_required",
            signature_version="s3v4",
        )
        self._http = httpx.AsyncClient(timeout=90.0)

    @property
    def bucket(self) -> str:
        return self._config.bucket

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
        key = normalize_object_key(key)
        if self._config.public_prefix:
            return f"{self._config.public_prefix.rstrip('/')}/{key}"
        return (
            f"{self._config.endpoint_url.rstrip('/')}/"
            f"{self._config.bucket.strip('/')}/{key}"
        )

    async def put_object(self, *, key: str, body: bytes, content_type: str) -> str:
        key = normalize_object_key(key)
        url = await self._presign("put_object", key, content_type=content_type)
        response = await self._request_with_retry(
            "PUT",
            url,
            content=body,
            headers={"Content-Type": content_type},
            error_cls=ObjectUploadFailedError,
            op="put",
            key=key,
        )
        if response.status_code >= 300:
            raise ObjectUploadFailedError(
                f"hippius put {key!r}: http {response.status_code}"
            )
        return self.public_url(key)

    async def presigned_get_url(
        self, *, key: str, expires_in: int = _PRESIGN_TTL_SECONDS
    ) -> str:
        """A time-bounded GET URL for one object (the bucket stays private)."""
        key = normalize_object_key(key)
        return await self._presign("get_object", key, expires_in=expires_in)

    async def list_objects(
        self,
        *,
        prefix: str = "",
        max_keys: int = 200,
        continuation_token: str | None = None,
    ) -> tuple[list[ObjectSummary], str | None]:
        """One ListObjectsV2 page via a presigned URL.

        Presigned like every other operation here: Hippius rejects SDK-direct
        calls with SignatureDoesNotMatch (module docstring), and a presigned
        GET of the bucket URL with the list parameters is the shape it accepts.
        Returns the page and the next continuation token (None when complete).
        """
        params: dict[str, str | int] = {
            "Bucket": self._config.bucket,
            "MaxKeys": max(1, min(int(max_keys), 1000)),
        }
        if prefix:
            params["Prefix"] = prefix
        if continuation_token:
            params["ContinuationToken"] = continuation_token
        async with self._session.client(
            "s3",
            endpoint_url=self._config.endpoint_url,
            use_ssl=True,
            config=self._client_config,
        ) as s3:
            url = await s3.generate_presigned_url(
                "list_objects_v2", Params=params, ExpiresIn=_PRESIGN_TTL_SECONDS
            )
        response = await self._request_with_retry(
            "GET", url, error_cls=ObjectDownloadFailedError, op="list", key=prefix
        )
        if response.status_code >= 300:
            raise ObjectDownloadFailedError(
                f"hippius list {prefix!r}: http {response.status_code}"
            )
        return _parse_list_objects(response.content)

    async def get_object(self, *, key: str) -> bytes:
        key = normalize_object_key(key)
        url = await self._presign("get_object", key)
        response = await self._request_with_retry(
            "GET",
            url,
            error_cls=ObjectDownloadFailedError,
            op="get",
            key=key,
        )
        if response.status_code == 404:
            raise ObjectNotFoundError(f"hippius object {key!r} not found")
        if response.status_code >= 300:
            raise ObjectDownloadFailedError(
                f"hippius get {key!r}: http {response.status_code}"
            )
        return response.content

    async def delete_object(self, *, key: str) -> None:
        key = normalize_object_key(key)
        url = await self._presign("delete_object", key)
        response = await self._request_with_retry(
            "DELETE",
            url,
            error_cls=ObjectUploadFailedError,
            op="delete",
            key=key,
        )
        if response.status_code not in {200, 204, 404}:
            raise ObjectUploadFailedError(
                f"hippius delete {key!r}: http {response.status_code}"
            )

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        error_cls: type[Exception],
        op: str,
        key: str,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                response = await self._http.request(
                    method, url, content=content, headers=headers
                )
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if not _should_retry(response.status_code, response.text):
                    return response
                last_error = error_cls(
                    f"hippius {op} {key!r}: http {response.status_code}"
                )
            if attempt + 1 >= _RETRY_ATTEMPTS:
                break
            await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
        if last_error is None:
            raise error_cls(f"hippius {op} {key!r} failed")
        if isinstance(last_error, error_cls):
            raise last_error
        raise error_cls(f"hippius {op} {key!r} failed: {last_error}") from last_error

    async def _presign(
        self,
        operation: str,
        key: str,
        *,
        content_type: str | None = None,
        expires_in: int = _PRESIGN_TTL_SECONDS,
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
            use_ssl=True,
            config=self._client_config,
        ) as s3:
            return await s3.generate_presigned_url(
                operation, Params=params, ExpiresIn=expires_in
            )


def _should_retry(status: int, body: str) -> bool:
    if status in _RETRY_STATUSES:
        return True
    if status == 402:
        lowered = body.lower()
        return (
            "uploadnotpermitted" in lowered
            and "failed to fetch billing balance" in lowered
        )
    return False


def create_hippius_client(
    config: HippiusConfig | None = None,
) -> HippiusClient | None:
    resolved = config if config is not None else parse_hippius_config_from_env()
    if resolved is None:
        return None
    return HippiusClient(resolved)


@dataclass(frozen=True)
class ObjectSummary:
    """One ListObjectsV2 row."""

    key: str
    size: int
    last_modified: str
    etag: str


def _parse_list_objects(body: bytes) -> tuple[list[ObjectSummary], str | None]:
    """Parse a ListObjectsV2 XML page (namespace-tolerant)."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(body)

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    objects: list[ObjectSummary] = []
    next_token: str | None = None
    truncated = False
    for element in root:
        name = local(element.tag)
        if name == "Contents":
            fields = {local(child.tag): (child.text or "") for child in element}
            objects.append(
                ObjectSummary(
                    key=fields.get("Key", ""),
                    size=int(fields.get("Size", "0") or 0),
                    last_modified=fields.get("LastModified", ""),
                    etag=fields.get("ETag", "").strip('"'),
                )
            )
        elif name == "NextContinuationToken":
            next_token = element.text or None
        elif name == "IsTruncated":
            truncated = (element.text or "").strip().lower() == "true"
    if not truncated:
        next_token = None
    return objects, next_token
