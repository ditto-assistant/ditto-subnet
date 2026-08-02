"""Unit tests for :class:`ditto.api_server.storage.client.S3StorageClient`.

aioboto3 sessions are mocked at the module boundary so the tests never
touch a real S3 endpoint. The mock returns an async-context-manager
wrapper around a fake s3 client whose ``put_object`` / ``head_object``
methods are :class:`AsyncMock` instances.
"""

from __future__ import annotations

import base64
import hashlib
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from ditto.api_server.storage import (
    ObjectDownloadFailedError,
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectUploadFailedError,
    S3StorageClient,
    StorageConfig,
    StoredObject,
)


class _ChunkedBody:
    """Mimic aiobotocore's ``StreamingBody``: ``read(amt)`` returns only the
    next buffered chunk (never more than the fake's internal chunk size), *not*
    the whole object. A single ``read`` therefore hands back a truncated prefix
    for any object spanning more than one chunk — exactly the behaviour that
    made source inspection 502 on real multi-chunk tarballs.
    """

    def __init__(self, data: bytes, *, chunk: int = 4) -> None:
        self._data = data
        self._chunk = chunk
        self._pos = 0

    async def read(self, amt: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        size = self._chunk if amt is None or amt < 0 else min(amt, self._chunk)
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


def _make_config(**overrides: object) -> StorageConfig:
    defaults: dict[str, object] = {
        "endpoint_url": "http://minio:9000",
        "bucket": "ditto-agents",
        "access_key": "minio",
        "secret_key": "miniominio",
        "region": "us-east-1",
        "use_tls": False,
    }
    defaults.update(overrides)
    return StorageConfig(**defaults)  # type: ignore[arg-type]


def _install_mock_session(
    client: S3StorageClient,
    *,
    put_side_effect: BaseException | None = None,
    head_side_effect: BaseException | None = None,
    head_result: dict[str, Any] | None = None,
    get_side_effect: BaseException | None = None,
    get_result: dict[str, Any] | None = None,
    presigned_url: str = "https://storage.example/signed",
) -> MagicMock:
    """Replace the client's aioboto3 session with a MagicMock + return it.

    The mock chain mirrors aioboto3's ``Session().client("s3") -> async ctx mgr
    -> s3 client`` shape. Tests inspect the s3 mock's ``put_object`` /
    ``head_object`` / ``get_object`` call args + behaviour.
    """
    s3_mock = MagicMock()
    s3_mock.put_object = AsyncMock(side_effect=put_side_effect)
    s3_mock.head_object = AsyncMock(
        side_effect=head_side_effect, return_value=head_result or {}
    )
    s3_mock.get_object = AsyncMock(
        side_effect=get_side_effect, return_value=get_result or {}
    )
    s3_mock.generate_presigned_url = AsyncMock(return_value=presigned_url)
    s3_mock.create_multipart_upload = AsyncMock(return_value={"UploadId": "upload-1"})
    s3_mock.complete_multipart_upload = AsyncMock()
    s3_mock.abort_multipart_upload = AsyncMock()
    s3_mock.delete_object = AsyncMock()
    s3_mock.list_objects_v2 = AsyncMock(return_value={"Contents": []})
    s3_mock.list_multipart_uploads = AsyncMock(return_value={"Uploads": []})

    @asynccontextmanager
    async def _client_ctx(*_args: object, **_kwargs: object):
        yield s3_mock

    session = MagicMock()
    session.client = MagicMock(side_effect=_client_ctx)
    client._session = session  # type: ignore[attr-defined]
    client._mock_s3 = s3_mock  # type: ignore[attr-defined]
    return session


class TestPutObject:
    async def test_disables_optional_botocore_checksums_for_s3_compatibility(self):
        client = S3StorageClient(_make_config())
        session = _install_mock_session(client)

        await client.put_object(key="abc/agent.tar.gz", body=b"x")

        client_config = session.client.call_args.kwargs["config"]
        assert client_config.request_checksum_calculation == "when_required"

    async def test_happy_path_returns_stored_object(self):
        client = S3StorageClient(_make_config())
        _install_mock_session(client)

        body = b"tarball-bytes"
        stored = await client.put_object(
            key="abc/agent.tar.gz",
            body=body,
            content_type="application/gzip",
        )

        assert isinstance(stored, StoredObject)
        assert stored.key == "abc/agent.tar.gz"
        assert stored.size_bytes == len(body)
        assert stored.sha256 == hashlib.sha256(body).hexdigest()

    async def test_passes_expected_kwargs(self):
        client = S3StorageClient(_make_config())
        _install_mock_session(client)

        await client.put_object(
            key="abc/agent.tar.gz", body=b"x", content_type="application/gzip"
        )

        kwargs = client._mock_s3.put_object.await_args.kwargs  # type: ignore[attr-defined]
        # Server-side encryption is enforced at the bucket level (default
        # encryption policy), not as a per-request header; minio without
        # KMS rejects per-request SSE while still applying the bucket
        # default to incoming objects.
        assert "ServerSideEncryption" not in kwargs
        assert kwargs["Bucket"] == "ditto-agents"
        assert kwargs["Key"] == "abc/agent.tar.gz"
        assert kwargs["ContentType"] == "application/gzip"
        assert kwargs["Body"] == b"x"
        assert kwargs["ContentMD5"] == base64.b64encode(
            hashlib.md5(b"x", usedforsecurity=False).digest()
        ).decode("ascii")

    async def test_default_content_type_octet_stream(self):
        client = S3StorageClient(_make_config())
        _install_mock_session(client)

        await client.put_object(key="anywhere", body=b"x")

        kwargs = client._mock_s3.put_object.await_args.kwargs  # type: ignore[attr-defined]
        assert kwargs["ContentType"] == "application/octet-stream"

    async def test_client_error_raises_typed(self):
        client = S3StorageClient(_make_config())
        _install_mock_session(
            client,
            put_side_effect=ClientError(
                error_response={"Error": {"Code": "AccessDenied"}},
                operation_name="PutObject",
            ),
        )

        with pytest.raises(ObjectUploadFailedError, match="AccessDenied"):
            await client.put_object(key="k", body=b"x")


class TestObjectExists:
    async def test_returns_true_when_head_succeeds(self):
        client = S3StorageClient(_make_config())
        _install_mock_session(client)

        assert await client.object_exists(key="abc/agent.tar.gz") is True

    @pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
    async def test_returns_false_on_404(self, code: str):
        client = S3StorageClient(_make_config())
        _install_mock_session(
            client,
            head_side_effect=ClientError(
                error_response={"Error": {"Code": code}},
                operation_name="HeadObject",
            ),
        )

        assert await client.object_exists(key="missing") is False

    async def test_non_404_error_raises_typed(self):
        client = S3StorageClient(_make_config())
        _install_mock_session(
            client,
            head_side_effect=ClientError(
                error_response={"Error": {"Code": "InternalError"}},
                operation_name="HeadObject",
            ),
        )

        with pytest.raises(ObjectUploadFailedError, match="InternalError"):
            await client.object_exists(key="boom")


class TestScreenedImageStorage:
    async def test_presigned_put_binds_size_type_and_metadata(self):
        client = S3StorageClient(_make_config())
        _install_mock_session(client)

        url = await client.presigned_put_url(
            key="abc/screened-image.tar",
            size_bytes=123,
            metadata={"sha256": "ab" * 32},
            expires_in=900,
        )

        assert url == "https://storage.example/signed"
        call = client._mock_s3.generate_presigned_url.await_args  # type: ignore[attr-defined]
        assert call.args == ("put_object",)
        assert call.kwargs["Params"] == {
            "Bucket": "ditto-agents",
            "Key": "abc/screened-image.tar",
            "ContentLength": 123,
            "ContentType": "application/x-tar",
            "Metadata": {"sha256": "ab" * 32},
        }
        assert call.kwargs["ExpiresIn"] == 900

    async def test_head_returns_size_and_user_metadata(self):
        client = S3StorageClient(_make_config())
        _install_mock_session(
            client,
            head_result={
                "ContentLength": 123,
                "Metadata": {"sha256": "ab" * 32, "image-id": "sha256:123"},
            },
        )

        result = await client.head_object(key="abc/screened-image.tar")

        assert result == ObjectMetadata(
            size_bytes=123,
            metadata={"sha256": "ab" * 32, "image-id": "sha256:123"},
        )

    async def test_multipart_create_part_complete_and_abort(self):
        client = S3StorageClient(_make_config())
        _install_mock_session(client)
        key = "abc/screened-images/session.tar"

        upload_id = await client.create_multipart_upload(
            key=key, metadata={"sha256": "ab" * 32}
        )
        part_url = await client.presigned_upload_part_url(
            key=key, upload_id=upload_id, part_number=1, expires_in=60
        )
        await client.complete_multipart_upload(
            key=key,
            upload_id=upload_id,
            parts=[{"PartNumber": 1, "ETag": '"etag"'}],
        )
        await client.abort_multipart_upload(key=key, upload_id="other")

        assert upload_id == "upload-1"
        assert part_url == "https://storage.example/signed"
        assert client._mock_s3.create_multipart_upload.await_args.kwargs[  # type: ignore[attr-defined]
            "Metadata"
        ] == {"sha256": "ab" * 32}
        assert client._mock_s3.generate_presigned_url.await_args.args == (  # type: ignore[attr-defined]
            "upload_part",
        )
        assert client._mock_s3.complete_multipart_upload.await_args.kwargs[  # type: ignore[attr-defined]
            "MultipartUpload"
        ] == {"Parts": [{"PartNumber": 1, "ETag": '"etag"'}]}
        client._mock_s3.abort_multipart_upload.assert_awaited_once()  # type: ignore[attr-defined]

    async def test_verify_streams_full_object_sha256(self):
        client = S3StorageClient(_make_config())
        body = b"full-multipart-object"
        _install_mock_session(client, get_result={"Body": _ChunkedBody(body, chunk=3)})

        verified = await client.verify_object_sha256(
            key="abc/screened-images/session.tar",
            expected_size_bytes=len(body),
        )

        assert verified.size_bytes == len(body)
        assert verified.sha256 == hashlib.sha256(body).hexdigest()

    async def test_head_missing_object_is_typed(self):
        client = S3StorageClient(_make_config())
        _install_mock_session(
            client,
            head_side_effect=ClientError(
                error_response={"Error": {"Code": "NoSuchKey"}},
                operation_name="HeadObject",
            ),
        )

        with pytest.raises(ObjectNotFoundError):
            await client.head_object(key="missing")


class TestGetObject:
    async def test_reassembles_object_spanning_multiple_chunks(self):
        client = S3StorageClient(_make_config())
        body = b"the-complete-stored-tarball-across-many-chunks"
        _install_mock_session(client, get_result={"Body": _ChunkedBody(body, chunk=4)})

        result = await client.get_object(key="a/agent.tar.gz", max_bytes=1024)

        # A single read would return only the first chunk; the drain loop must
        # return the whole object so its digest matches the stored artifact
        # (a truncated body is what surfaced to operators as a 502).
        assert result == body
        assert hashlib.sha256(result).hexdigest() == hashlib.sha256(body).hexdigest()

    async def test_reads_object_smaller_than_one_chunk(self):
        client = S3StorageClient(_make_config())
        body = b"tiny"
        _install_mock_session(client, get_result={"Body": _ChunkedBody(body, chunk=64)})

        assert await client.get_object(key="k", max_bytes=1024) == body

    async def test_rejects_object_exceeding_max_bytes(self):
        client = S3StorageClient(_make_config())
        _install_mock_session(
            client, get_result={"Body": _ChunkedBody(b"x" * 40, chunk=4)}
        )

        with pytest.raises(ObjectDownloadFailedError, match="exceeded bound"):
            await client.get_object(key="k", max_bytes=8)

    async def test_client_error_raises_typed(self):
        client = S3StorageClient(_make_config())
        _install_mock_session(
            client,
            get_side_effect=ClientError(
                error_response={"Error": {"Code": "NoSuchKey"}},
                operation_name="GetObject",
            ),
        )

        with pytest.raises(ObjectDownloadFailedError, match="NoSuchKey"):
            await client.get_object(key="missing", max_bytes=1024)


class TestContextManager:
    async def test_works_as_async_context_manager(self):
        client = S3StorageClient(_make_config())
        async with client as entered:
            assert entered is client
