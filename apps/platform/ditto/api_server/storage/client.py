"""S3-compatible object store client used by the upload pipeline."""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import TYPE_CHECKING

from ditto.api_server.storage.errors import (
    ObjectDownloadFailedError,
    ObjectNotFoundError,
    ObjectUploadFailedError,
)
from ditto.api_server.storage.models import (
    ListedObject,
    MultipartUpload,
    ObjectMetadata,
    StoredObject,
    VerifiedObject,
)

if TYPE_CHECKING:
    from types import TracebackType

    from ditto.api_server.storage.models import StorageConfig

logger = logging.getLogger(__name__)

# Per-read chunk size when draining a download stream. The stream is read in a
# loop (a single read does not return the whole object), so this only bounds how
# much is pulled per await, not the total; the caller's ``max_bytes`` bounds that.
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


class S3StorageClient:
    """Async wrapper around aioboto3's S3 client.

    Speaks the generic S3 API so the same client works against minio in
    dev compose, AWS S3 in prod, Cloudflare R2, Backblaze B2, or any
    other S3-compatible endpoint via :class:`StorageConfig.endpoint_url`.

    The lifespan owns one of these per process and reuses the underlying
    aioboto3 :class:`aioboto3.Session` across requests. Each call to
    :meth:`put_object` / :meth:`object_exists` opens a short-lived
    ``async with self._session.client("s3", ...)`` block; aiobotocore
    sets up + tears down a fresh client (and TCP connection) per call.
    At MVP scale this is fine; if upload volume ever makes the per-call
    handshake material, cache a long-lived client in :meth:`__aenter__`
    + close in :meth:`__aexit__`.

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
        from botocore.config import Config

        self._config = config
        # Botocore's optional request checksums use an ``aws-chunked`` payload
        # encoding that GCS's S3-compatible XML API does not accept. The
        # resulting PutObject failure is misleadingly reported by GCS as
        # SignatureDoesNotMatch. Required checksums remain enabled, while plain
        # PutObject requests use the interoperable content-length payload. Each
        # upload also supplies Content-MD5 for server-side integrity validation.
        self._client_config = Config(
            request_checksum_calculation="when_required",
        )
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

    @property
    def public_bucket(self) -> str | None:
        """The transparency-mirror bucket, or ``None`` when publishing is off."""
        return self._config.public_bucket

    async def put_object(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str = "application/octet-stream",
        bucket: str | None = None,
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

        target = bucket or self._config.bucket
        try:
            async with self._session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
                use_ssl=self._config.use_tls,
                config=self._client_config,
            ) as s3:
                await s3.put_object(
                    Bucket=target,
                    Key=key,
                    Body=body,
                    ContentType=content_type,
                    ContentMD5=base64.b64encode(
                        hashlib.md5(body, usedforsecurity=False).digest()
                    ).decode("ascii"),
                )
        except (ClientError, BotoCoreError) as e:
            raise ObjectUploadFailedError(
                f"put_object failed: bucket={target!r} key={key!r} cause={e}"
            ) from e

        sha256 = hashlib.sha256(body).hexdigest()
        logger.info(
            f"stored object bucket={target} key={key} "
            f"size_bytes={len(body)} sha256={sha256}"
        )
        return StoredObject(key=key, size_bytes=len(body), sha256=sha256)

    async def presigned_get_url(
        self,
        *,
        key: str,
        expires_in: int = 300,
        attachment_filename: str | None = None,
    ) -> str:
        """Return a pre-signed GET URL the validator daemon can stream from.

        The URL embeds a time-limited signature, so the bucket can stay
        private while the daemon pulls the tarball directly from object
        storage (no proxying bytes through the API). ``expires_in`` bounds
        the validity window in seconds. ``attachment_filename`` signs a
        ``Content-Disposition: attachment`` response override so a browser
        hitting the URL saves the object under that name instead of
        rendering it or falling back to the key's basename.

        Generating a pre-signed URL is a local signing operation — no
        network round trip and no existence check — so a URL for a missing
        key is still returned; the daemon's GET then 404s. Callers that need
        existence semantics check the ``agents`` row first.

        Raises:
            ObjectUploadFailedError: When the underlying client cannot be
                constructed or the signing call fails.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            async with self._session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
                use_ssl=self._config.use_tls,
                config=self._client_config,
            ) as s3:
                params: dict[str, str] = {"Bucket": self._config.bucket, "Key": key}
                if attachment_filename is not None:
                    params["ResponseContentDisposition"] = (
                        f'attachment; filename="{attachment_filename}"'
                    )
                return await s3.generate_presigned_url(
                    "get_object",
                    Params=params,
                    ExpiresIn=expires_in,
                )
        except (ClientError, BotoCoreError) as e:
            raise ObjectUploadFailedError(
                f"presigned_get_url failed: bucket={self._config.bucket!r} "
                f"key={key!r} cause={e}"
            ) from e

    async def presigned_put_url(
        self,
        *,
        key: str,
        size_bytes: int,
        metadata: dict[str, str],
        content_type: str = "application/x-tar",
        expires_in: int = 300,
    ) -> str:
        """Return a pre-signed PUT constrained to size, type, and metadata."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            async with self._session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
                use_ssl=self._config.use_tls,
                config=self._client_config,
            ) as s3:
                return await s3.generate_presigned_url(
                    "put_object",
                    Params={
                        "Bucket": self._config.bucket,
                        "Key": key,
                        "ContentLength": size_bytes,
                        "ContentType": content_type,
                        "Metadata": metadata,
                    },
                    ExpiresIn=expires_in,
                )
        except (ClientError, BotoCoreError) as error:
            raise ObjectUploadFailedError(
                f"presigned_put_url failed: bucket={self._config.bucket!r} "
                f"key={key!r} cause={error}"
            ) from error

    async def create_multipart_upload(
        self,
        *,
        key: str,
        metadata: dict[str, str],
        content_type: str = "application/x-tar",
    ) -> str:
        """Create a multipart upload and return its opaque storage upload id."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            async with self._session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
                use_ssl=self._config.use_tls,
                config=self._client_config,
            ) as s3:
                response = await s3.create_multipart_upload(
                    Bucket=self._config.bucket,
                    Key=key,
                    ContentType=content_type,
                    Metadata=metadata,
                )
        except (ClientError, BotoCoreError) as error:
            raise ObjectUploadFailedError(
                f"create_multipart_upload failed: key={key!r} cause={error}"
            ) from error
        upload_id = str(response.get("UploadId", ""))
        if not upload_id:
            raise ObjectUploadFailedError("multipart upload returned no upload id")
        return upload_id

    async def presigned_upload_part_url(
        self, *, key: str, upload_id: str, part_number: int, expires_in: int = 300
    ) -> str:
        """Return a short-lived URL for one numbered multipart part."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            async with self._session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
                use_ssl=self._config.use_tls,
                config=self._client_config,
            ) as s3:
                return await s3.generate_presigned_url(
                    "upload_part",
                    Params={
                        "Bucket": self._config.bucket,
                        "Key": key,
                        "UploadId": upload_id,
                        "PartNumber": part_number,
                    },
                    ExpiresIn=expires_in,
                )
        except (ClientError, BotoCoreError) as error:
            raise ObjectUploadFailedError(
                f"presigned_upload_part_url failed: key={key!r} "
                f"part={part_number} cause={error}"
            ) from error

    async def complete_multipart_upload(
        self, *, key: str, upload_id: str, parts: list[dict[str, int | str]]
    ) -> None:
        """Complete a multipart upload with caller-observed part ETags."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            async with self._session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
                use_ssl=self._config.use_tls,
                config=self._client_config,
            ) as s3:
                await s3.complete_multipart_upload(
                    Bucket=self._config.bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey", "NoSuchUpload", "NotFound"}:
                raise ObjectNotFoundError(
                    f"multipart upload is unavailable: key={key!r}"
                ) from error
            raise ObjectUploadFailedError(
                f"complete_multipart_upload failed: key={key!r} cause={error}"
            ) from error
        except BotoCoreError as error:
            raise ObjectUploadFailedError(
                f"complete_multipart_upload failed: key={key!r} cause={error}"
            ) from error

    async def abort_multipart_upload(self, *, key: str, upload_id: str) -> None:
        """Abort an incomplete multipart upload; missing uploads are idempotent."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            async with self._session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
                use_ssl=self._config.use_tls,
                config=self._client_config,
            ) as s3:
                await s3.abort_multipart_upload(
                    Bucket=self._config.bucket, Key=key, UploadId=upload_id
                )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code not in {"404", "NoSuchUpload", "NotFound"}:
                raise ObjectUploadFailedError(
                    f"abort_multipart_upload failed: key={key!r} cause={error}"
                ) from error
        except BotoCoreError as error:
            raise ObjectUploadFailedError(
                f"abort_multipart_upload failed: key={key!r} cause={error}"
            ) from error

    async def delete_object(self, *, key: str) -> None:
        """Delete an object idempotently."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            async with self._session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
                use_ssl=self._config.use_tls,
                config=self._client_config,
            ) as s3:
                await s3.delete_object(Bucket=self._config.bucket, Key=key)
        except (ClientError, BotoCoreError) as error:
            raise ObjectUploadFailedError(
                f"delete_object failed: key={key!r} cause={error}"
            ) from error

    async def list_objects(self, *, prefix: str) -> list[ListedObject]:
        """List object keys and modification times under ``prefix``."""
        from botocore.exceptions import BotoCoreError, ClientError

        objects: list[ListedObject] = []
        token: str | None = None
        try:
            while True:
                async with self._session.client(
                    "s3",
                    endpoint_url=self._config.endpoint_url,
                    use_ssl=self._config.use_tls,
                    config=self._client_config,
                ) as s3:
                    kwargs: dict[str, object] = {
                        "Bucket": self._config.bucket,
                        "Prefix": prefix,
                    }
                    if token is not None:
                        kwargs["ContinuationToken"] = token
                    response = await s3.list_objects_v2(**kwargs)
                objects.extend(
                    ListedObject(
                        key=str(item["Key"]), last_modified=item["LastModified"]
                    )
                    for item in response.get("Contents", [])
                )
                if not response.get("IsTruncated"):
                    return objects
                token = str(response["NextContinuationToken"])
        except (ClientError, BotoCoreError, KeyError) as error:
            raise ObjectUploadFailedError(
                f"list_objects failed: prefix={prefix!r} cause={error}"
            ) from error

    async def list_multipart_uploads(self, *, prefix: str) -> list[MultipartUpload]:
        """List all incomplete multipart uploads under ``prefix``."""
        from botocore.exceptions import BotoCoreError, ClientError

        uploads: list[MultipartUpload] = []
        key_marker: str | None = None
        upload_marker: str | None = None
        try:
            while True:
                async with self._session.client(
                    "s3",
                    endpoint_url=self._config.endpoint_url,
                    use_ssl=self._config.use_tls,
                    config=self._client_config,
                ) as s3:
                    kwargs: dict[str, object] = {
                        "Bucket": self._config.bucket,
                        "Prefix": prefix,
                    }
                    if key_marker is not None:
                        kwargs["KeyMarker"] = key_marker
                    if upload_marker is not None:
                        kwargs["UploadIdMarker"] = upload_marker
                    response = await s3.list_multipart_uploads(**kwargs)
                uploads.extend(
                    MultipartUpload(
                        key=str(item["Key"]),
                        upload_id=str(item["UploadId"]),
                        initiated_at=item["Initiated"],
                    )
                    for item in response.get("Uploads", [])
                )
                if not response.get("IsTruncated"):
                    return uploads
                key_marker = str(response["NextKeyMarker"])
                upload_marker = str(response["NextUploadIdMarker"])
        except (ClientError, BotoCoreError, KeyError) as error:
            raise ObjectUploadFailedError(
                f"list_multipart_uploads failed: prefix={prefix!r} cause={error}"
            ) from error

    async def verify_object_sha256(
        self, *, key: str, expected_size_bytes: int
    ) -> VerifiedObject:
        """Stream all final bytes and compute their full archive SHA-256.

        Multipart ETags and per-part checksums are deliberately not treated as
        equivalent to this whole-object verification.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        digest = hashlib.sha256()
        total = 0
        try:
            async with self._session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
                use_ssl=self._config.use_tls,
                config=self._client_config,
            ) as s3:
                response = await s3.get_object(Bucket=self._config.bucket, Key=key)
                stream = response["Body"]
                while True:
                    chunk = await stream.read(_DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected_size_bytes:
                        break
                    digest.update(chunk)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise ObjectNotFoundError(
                    f"object is unavailable: key={key!r}"
                ) from error
            raise ObjectDownloadFailedError(
                f"verify_object_sha256 failed: key={key!r} cause={error}"
            ) from error
        except BotoCoreError as error:
            raise ObjectDownloadFailedError(
                f"verify_object_sha256 failed: key={key!r} cause={error}"
            ) from error
        return VerifiedObject(size_bytes=total, sha256=digest.hexdigest())

    async def head_object(self, *, key: str) -> ObjectMetadata:
        """Return object size and user metadata for a direct upload."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            async with self._session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
                use_ssl=self._config.use_tls,
                config=self._client_config,
            ) as s3:
                response = await s3.head_object(Bucket=self._config.bucket, Key=key)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise ObjectNotFoundError(
                    f"object is unavailable: key={key!r}"
                ) from error
            raise ObjectUploadFailedError(
                f"head_object failed: bucket={self._config.bucket!r} "
                f"key={key!r} cause={error}"
            ) from error
        except BotoCoreError as error:
            raise ObjectUploadFailedError(
                f"head_object failed: bucket={self._config.bucket!r} "
                f"key={key!r} cause={error}"
            ) from error
        return ObjectMetadata(
            size_bytes=int(response.get("ContentLength", -1)),
            metadata={str(k): str(v) for k, v in response.get("Metadata", {}).items()},
        )

    async def get_object(self, *, key: str, max_bytes: int) -> bytes:
        """Download ``key`` into memory, bounded to ``max_bytes``.

        Serves the operator source-inspection endpoints, which extract
        bounded excerpts from an uploaded tarball server-side. Upload already
        caps tarballs (20 MiB by default), so an in-memory read is fine; the
        explicit bound keeps a mis-sized object from ballooning the process.

        Raises:
            ObjectDownloadFailedError: When the object is missing, exceeds
                ``max_bytes``, or the underlying S3 call fails.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            async with self._session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
                use_ssl=self._config.use_tls,
                config=self._client_config,
            ) as s3:
                response = await s3.get_object(Bucket=self._config.bucket, Key=key)
                # aiobotocore's StreamingBody wraps an aiohttp reader whose
                # read(n) returns only the NEXT buffered chunk (up to n), not
                # the whole object. A single read(max_bytes + 1) therefore
                # hands back a truncated prefix for any object large enough to
                # span multiple chunks, whose sha256 never matches the stored
                # digest (surfacing to operators as a 502 on every source
                # inspection). Drain the stream so the returned bytes are the
                # COMPLETE object, aborting as soon as the running total crosses
                # the bound so a mis-sized object can't balloon the process.
                stream = response["Body"]
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = await stream.read(_DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ObjectDownloadFailedError(
                            f"get_object exceeded bound: "
                            f"bucket={self._config.bucket!r} "
                            f"key={key!r} max_bytes={max_bytes}"
                        )
                    chunks.append(chunk)
        except (ClientError, BotoCoreError) as e:
            raise ObjectDownloadFailedError(
                f"get_object failed: bucket={self._config.bucket!r} "
                f"key={key!r} cause={e}"
            ) from e
        return b"".join(chunks)

    async def object_exists(self, *, key: str, bucket: str | None = None) -> bool:
        """Return ``True`` iff a HEAD against ``key`` succeeds.

        Used by integration tests, the content-addressed transcript upload
        (idempotence check), and future janitor sweeps. ``bucket`` defaults to
        the main bucket, mirroring :meth:`put_object`. Returns ``False`` on
        404, raises :class:`ObjectUploadFailedError`-style errors for any
        other failure (wrapped via :class:`StorageError`).

        Raises:
            ObjectUploadFailedError: When the endpoint returns an error
                other than 404.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        target = bucket or self._config.bucket
        try:
            async with self._session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
                use_ssl=self._config.use_tls,
                config=self._client_config,
            ) as s3:
                await s3.head_object(Bucket=target, Key=key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise ObjectUploadFailedError(
                f"head_object failed: bucket={target!r} key={key!r} cause={e}"
            ) from e
        except BotoCoreError as e:
            raise ObjectUploadFailedError(
                f"head_object failed: bucket={target!r} key={key!r} cause={e}"
            ) from e
        return True
