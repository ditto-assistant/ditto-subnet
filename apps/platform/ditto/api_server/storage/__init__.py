"""S3-compatible object storage for upload tarballs.

Wraps aioboto3's S3 client. The same client talks to minio in dev
compose and AWS S3 (or R2 / B2 / any S3-compatible endpoint) in prod
via the ``STORAGE_ENDPOINT_URL`` env var. Validators retrieve agent
tars through the API (presigned URL, next PR), never directly from
this client.

Usage:
    from ditto.api_server.storage import (
        create_storage_client,
        parse_storage_config_from_env,
    )

    config = parse_storage_config_from_env()
    async with create_storage_client(config) as storage:
        stored = await storage.put_object(
            key=f"{agent_id}/agent.tar.gz",
            body=tar_bytes,
            content_type="application/gzip",
        )
"""

from __future__ import annotations

from ditto.api_server.storage.client import S3StorageClient
from ditto.api_server.storage.errors import (
    ObjectDownloadFailedError,
    ObjectNotFoundError,
    ObjectUploadFailedError,
    StorageConfigurationError,
    StorageError,
)
from ditto.api_server.storage.factory import create_storage_client
from ditto.api_server.storage.models import (
    ListedObject,
    MultipartUpload,
    ObjectMetadata,
    StorageConfig,
    StoredObject,
    VerifiedObject,
    parse_storage_config_from_env,
)

__all__ = [
    # Main components
    "S3StorageClient",
    # Configuration
    "StorageConfig",
    "parse_storage_config_from_env",
    # Result models
    "StoredObject",
    "ObjectMetadata",
    "VerifiedObject",
    "ListedObject",
    "MultipartUpload",
    # Errors
    "ObjectDownloadFailedError",
    "ObjectNotFoundError",
    "ObjectUploadFailedError",
    "StorageConfigurationError",
    "StorageError",
    # Factory
    "create_storage_client",
]
