"""Configuration + result dataclasses for the storage client."""

from __future__ import annotations

import os
from dataclasses import dataclass

from ditto.api_server.storage.errors import StorageConfigurationError


@dataclass(frozen=True)
class StorageConfig:
    """Resolved configuration for :class:`S3StorageClient`.

    Speaks the generic S3 API: the same client talks to minio in
    compose, AWS S3 in prod, or any other S3-compatible endpoint via
    ``endpoint_url``.
    """

    endpoint_url: str
    """S3 endpoint URL. ``http://minio:9000`` in dev compose;
    unset / overridden to the AWS regional endpoint in prod
    (``STORAGE_ENDPOINT_URL``)."""

    bucket: str
    """Bucket name where agent tarballs are stored (``STORAGE_BUCKET``)."""

    access_key: str
    """Access key id (``STORAGE_ACCESS_KEY``)."""

    secret_key: str
    """Secret access key (``STORAGE_SECRET_KEY``). Logged with redaction at boot."""

    region: str = "us-east-1"
    """Region name (``STORAGE_REGION``). minio ignores; AWS S3 requires."""

    use_tls: bool = False
    """Whether the endpoint speaks TLS (``STORAGE_USE_TLS``). ``False``
    for dev compose, ``True`` in prod."""


@dataclass(frozen=True)
class StoredObject:
    """Outcome of a successful :meth:`S3StorageClient.put_object` call."""

    key: str
    """Object key written. Echoed back so caller can log the canonical key."""

    size_bytes: int
    """Bytes actually written. Sanity-check against caller's expectation."""

    sha256: str
    """Hex sha256 of the body the client uploaded. Lets callers correlate
    the stored object with the tar bytes they previously hashed."""


def _parse_bool(name: str, raw: str) -> bool:
    if raw.lower() in {"true", "1", "yes", "on"}:
        return True
    if raw.lower() in {"false", "0", "no", "off"}:
        return False
    raise StorageConfigurationError(
        f"{name} must be a boolean (true/false), got {raw!r}"
    )


def parse_storage_config_from_env() -> StorageConfig:
    """Build :class:`StorageConfig` from ``STORAGE_*`` env vars.

    Raises:
        StorageConfigurationError: When a required env var is missing
            or ``STORAGE_USE_TLS`` cannot be parsed as bool.
    """
    endpoint_url = os.environ.get("STORAGE_ENDPOINT_URL", "")
    bucket = os.environ.get("STORAGE_BUCKET", "")
    access_key = os.environ.get("STORAGE_ACCESS_KEY", "")
    secret_key = os.environ.get("STORAGE_SECRET_KEY", "")
    region = os.environ.get("STORAGE_REGION", "us-east-1")
    use_tls_raw = os.environ.get("STORAGE_USE_TLS", "false")

    missing = [
        name
        for name, value in (
            ("STORAGE_ENDPOINT_URL", endpoint_url),
            ("STORAGE_BUCKET", bucket),
            ("STORAGE_ACCESS_KEY", access_key),
            ("STORAGE_SECRET_KEY", secret_key),
        )
        if not value
    ]
    if missing:
        raise StorageConfigurationError(
            f"required storage env vars unset: {', '.join(missing)}"
        )

    return StorageConfig(
        endpoint_url=endpoint_url,
        bucket=bucket,
        access_key=access_key,
        secret_key=secret_key,
        region=region,
        use_tls=_parse_bool("STORAGE_USE_TLS", use_tls_raw),
    )
