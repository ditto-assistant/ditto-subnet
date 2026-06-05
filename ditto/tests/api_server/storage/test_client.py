"""Unit tests for :class:`ditto.api_server.storage.client.S3StorageClient`.

aioboto3 sessions are mocked at the module boundary so the tests never
touch a real S3 endpoint. The mock returns an async-context-manager
wrapper around a fake s3 client whose ``put_object`` / ``head_object``
methods are :class:`AsyncMock` instances.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from ditto.api_server.storage import (
    ObjectUploadFailedError,
    S3StorageClient,
    StorageConfig,
    StoredObject,
)


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
) -> MagicMock:
    """Replace the client's aioboto3 session with a MagicMock + return it.

    The mock chain mirrors aioboto3's ``Session().client("s3") -> async ctx mgr
    -> s3 client`` shape. Tests inspect the s3 mock's ``put_object`` /
    ``head_object`` call args + behaviour.
    """
    s3_mock = MagicMock()
    s3_mock.put_object = AsyncMock(side_effect=put_side_effect)
    s3_mock.head_object = AsyncMock(
        side_effect=head_side_effect, return_value=head_result or {}
    )

    @asynccontextmanager
    async def _client_ctx(*_args: object, **_kwargs: object):
        yield s3_mock

    session = MagicMock()
    session.client = MagicMock(side_effect=_client_ctx)
    client._session = session  # type: ignore[attr-defined]
    client._mock_s3 = s3_mock  # type: ignore[attr-defined]
    return session


class TestPutObject:
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


class TestContextManager:
    async def test_works_as_async_context_manager(self):
        client = S3StorageClient(_make_config())
        async with client as entered:
            assert entered is client
