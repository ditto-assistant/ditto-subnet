"""Exception hierarchy for :mod:`ditto.api_server.storage`."""

from __future__ import annotations


class StorageError(Exception):
    """Base exception for :mod:`ditto.api_server.storage`."""


# --- Configuration ---


class StorageConfigurationError(StorageError):
    """Raised when storage config cannot be resolved at boot.

    This can happen when:
    - A required ``STORAGE_*`` env var is missing or empty.
    - ``STORAGE_USE_TLS`` is set to a value that cannot be parsed as bool.
    - ``STORAGE_ENDPOINT_URL`` is set but does not look like an http(s)
      URL when one is expected.
    """


# --- Object operations ---


class ObjectUploadFailedError(StorageError):
    """Raised when a put_object or head_object call cannot complete.

    This can happen when:
    - The S3 endpoint is unreachable (network, DNS, TLS handshake).
    - The bucket does not exist or the access key lacks the required
      ``s3:PutObject`` / ``s3:GetObject`` permission on it.
    - The endpoint rejects the request with a 4xx or 5xx response
      that is not a routine 404 against a missing key.
    - The body exceeds the maximum object size configured upstream.
    """
