"""Bounded source and credential helpers for the live fan-out shadow worker."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

import httpx

_MAX_SOURCE_BYTES = 20 * 1024 * 1024
_MAX_PROVIDER_KEY_BYTES = 16 * 1024


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"missing {name}")
    return value


def _stage_source_review_secret(key_file: str) -> str:
    """Copy group-readable secret mounts into the job's private tmpfs."""
    source = Path(key_file)
    source_stat = source.stat()
    if not stat.S_ISREG(source_stat.st_mode):
        raise OSError("source review API key file is not a regular file")
    if not source_stat.st_mode & 0o077:
        return key_file
    if source_stat.st_size > _MAX_PROVIDER_KEY_BYTES:
        raise OSError("source review API key file is too large")

    credential_path = os.environ.get("SCREENER_NODE_CREDENTIAL_FILE")
    if not credential_path:
        raise OSError("SCREENER_NODE_CREDENTIAL_FILE is required for source review")
    target = Path(credential_path).with_name("source-review-api-key.staged")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(source, read_flags), "rb") as handle:
        value = handle.read(_MAX_PROVIDER_KEY_BYTES + 1)
    if len(value) > _MAX_PROVIDER_KEY_BYTES:
        raise OSError("source review API key file is too large")

    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        with os.fdopen(os.open(target, write_flags, 0o600), "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    os.environ["SCREENER_SOURCE_REVIEW_API_KEY_FILE"] = str(target)
    return str(target)


async def _download_verified(
    client: httpx.AsyncClient, url: str, expected_sha256: str
) -> str:
    descriptor, path = tempfile.mkstemp(prefix="ditto-source-review-", suffix=".tgz")
    os.fchmod(descriptor, 0o600)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(descriptor, "wb") as handle:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > _MAX_SOURCE_BYTES:
                        raise ValueError("source archive exceeded its bound")
                    digest.update(chunk)
                    handle.write(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ValueError("source archive digest mismatch")
        return path
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise
