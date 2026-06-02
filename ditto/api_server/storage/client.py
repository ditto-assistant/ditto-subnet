"""S3-compatible object store client used by the upload pipeline."""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from ditto.api_server.storage.errors import ObjectUploadFailedError
from ditto.api_server.storage.models import StoredObject

if TYPE_CHECKING:
    from types import TracebackType

    from ditto.api_server.storage.models import StorageConfig

logger = logging.getLogger(__name__)


class S3StorageClient:
    """Async wrapper around aioboto3's S3 client.

    Speaks the generic S3 API so the same client works against minio in
    dev compose, AWS S3 in prod, Cloudflare R2, Backblaze B2, or any
    other S3-compatible endpoint via :class:`StorageConfig.endpoint_url`.

    The lifespan owns one of these per process and reuses the underlying
    aioboto3 session across requests. Per-call work goes through a
    short-lived ``async with self._session.client(...)`` block; aioboto3
    pools the connection internally.

    Usage:
        async with create_storage_client(config) as storage:
            stored = await storage.put_object(
                key=f"{agent_id}/agent.tar.gz",
                body=tar_bytes,
                content_type="application/gzip",
            )
    """

    def __init__(self, config: StorageConfig) -> None:
        # Lazy import: aioboto3 + boto3 + botocore are heavy. Defer to
        # actual instantiation so import-time cost is paid only by the
        # api_server lifespan, not test discovery.
        import aioboto3

        self._config = config
        self._session = aioboto3.Session(
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            region_name=config.region,
        )

    async def __aenter__(self) -> S3StorageClient:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        return None

    async def put_object(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str = "application/octet-stream",
    ) -> StoredObject:
        """Upload ``body`` to ``key``.

        Server-side encryption is enforced at the BUCKET level via
        default-encryption policy rather than per-request, because
        per-request ``ServerSideEncryption`` headers are rejected by
        minio without KMS config. Bucket-level default encryption (set
        via Terraform / mc encrypt for minio / S3 default encryption)
        applies transparently to every object written here.

        Raises:
            ObjectUploadFailedError: When the underlying S3 call raises
                ``botocore.exceptions.ClientError`` or the endpoint is
                unreachable.
        """
        # Lazy: only botocore exceptions are needed here.
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            async with self._session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
                use_ssl=self._config.use_tls,
            ) as s3:
                await s3.put_object(
                    Bucket=self._config.bucket,
                    Key=key,
                    Body=body,
                    ContentType=content_type,
                )
        except (ClientError, BotoCoreError) as e:
            raise ObjectUploadFailedError(
                f"put_object failed: bucket={self._config.bucket!r} "
                f"key={key!r} cause={e}"
            ) from e

        sha256 = hashlib.sha256(body).hexdigest()
        logger.info(
            f"stored object bucket={self._config.bucket} key={key} "
            f"size_bytes={len(body)} sha256={sha256}"
        )
        return StoredObject(key=key, size_bytes=len(body), sha256=sha256)

    async def object_exists(self, *, key: str) -> bool:
        """Return ``True`` iff a HEAD against ``key`` succeeds.

        Used by integration tests + future janitor sweeps. Returns
        ``False`` on 404, raises :class:`ObjectUploadFailedError`-style
        errors for any other failure (wrapped via :class:`StorageError`).

        Raises:
            ObjectUploadFailedError: When the endpoint returns an error
                other than 404.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            async with self._session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
                use_ssl=self._config.use_tls,
            ) as s3:
                await s3.head_object(Bucket=self._config.bucket, Key=key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise ObjectUploadFailedError(
                f"head_object failed: bucket={self._config.bucket!r} "
                f"key={key!r} cause={e}"
            ) from e
        except BotoCoreError as e:
            raise ObjectUploadFailedError(
                f"head_object failed: bucket={self._config.bucket!r} "
                f"key={key!r} cause={e}"
            ) from e
        return True
