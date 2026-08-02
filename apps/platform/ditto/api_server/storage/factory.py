"""Factory for the S3 storage client."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ditto.api_server.storage.client import S3StorageClient
from ditto.api_server.storage.models import parse_storage_config_from_env

if TYPE_CHECKING:
    from ditto.api_server.storage.models import StorageConfig


def create_storage_client(
    config: StorageConfig | None = None,
) -> S3StorageClient:
    """Construct an :class:`S3StorageClient` against the supplied config.

    When ``config`` is ``None``, reads ``STORAGE_*`` env vars via
    :func:`parse_storage_config_from_env`. The api_server lifespan
    passes the resolved :class:`ApiServerConfig.storage` directly so
    boot-time env parsing happens exactly once.
    """
    if config is None:
        config = parse_storage_config_from_env()
    return S3StorageClient(config)
