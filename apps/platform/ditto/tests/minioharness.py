"""Real object-storage provisioning for the test suite.

The same argument :mod:`ditto.tests.pgharness` makes for Postgres, one tier
down. Four ``integration`` tests exercise the real upload path -- stream the
tarball, ``put_object``, re-verify the SHA-256, then commit the DB
transaction -- and they need an S3 endpoint that actually stores bytes. A
mocked store cannot fail the way a real one does, and the store-before-commit
ordering those tests pin is only meaningful against a real one.

Until now that endpoint came from ``docker compose`` via ``make stack-up``.
That left the suite green only for someone who knew to run it, which is the
same category of tribal knowledge as "remember to export
``DITTO_UPLOAD_PAYMENT_ADDRESS``" -- and produced the same result, a red
suite on a fresh clone.

So this mirrors ``pgharness`` exactly:

* one **ambient** container, started on demand and never torn down, so the
  second ``pytest`` of the day pays no container cost;
* a **test-only** host port (19000, not compose's 9000), so a test run can
  never be pointed at, or confused with, a bucket holding anything anybody
  cares about;
* a file lock around startup, because ``-n auto`` fans out before anything
  has touched Docker and every worker would otherwise race to ``docker run``
  the same container name.

Environment:

``DITTO_TEST_MINIO_ENDPOINT``
    Endpoint of an already-running S3-compatible store (a CI service
    container). When set, no Docker command is ever issued. Note that an
    explicit ``STORAGE_ENDPOINT_URL`` -- which is what CI sets -- also
    short-circuits this harness entirely; see :func:`resolve_storage_env`.
``DITTO_REQUIRE_OBJECT_STORAGE``
    ``1`` turns any provisioning failure into a hard error instead of a
    skip, for the same reason ``DITTO_REQUIRE_POSTGRES`` exists.
``DITTO_TEST_MINIO_PORT`` / ``_CONTAINER`` / ``_IMAGE``
    Override the ambient container's host port, name, and image.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

CONTAINER_NAME = os.environ.get(
    "DITTO_TEST_MINIO_CONTAINER", "ditto-platform-test-minio"
)
"""Long-lived, named container. Never torn down by the test run."""

CONTAINER_IMAGE = os.environ.get("DITTO_TEST_MINIO_IMAGE", "minio/minio:latest")
"""Matches ``docker-compose.yml``'s ``minio`` service and CI's container."""

HOST_PORT = int(os.environ.get("DITTO_TEST_MINIO_PORT", "19000"))
"""Deliberately not 9000 (the compose dev store), so the suite cannot write
test objects into a bucket a developer is looking at."""

ACCESS_KEY = "minio"
SECRET_KEY = "miniominio"
BUCKET = "ditto-agents"


class ObjectStorageUnavailable(RuntimeError):
    """Raised when a real object store could not be provisioned."""


def _require() -> bool:
    return os.environ.get("DITTO_REQUIRE_OBJECT_STORAGE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=180
    )


def _container_state() -> str:
    """``running`` / ``exited`` / ``""`` when the container does not exist."""
    proc = _docker("inspect", "--format", "{{.State.Status}}", CONTAINER_NAME)
    return proc.stdout.strip() if proc.returncode == 0 else ""


@contextlib.contextmanager
def _startup_lock() -> Iterator[None]:
    """Serialise container startup across every worker on this machine."""
    path = Path(tempfile.gettempdir()) / f"{CONTAINER_NAME}.startup.lock"
    handle = path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _start_container() -> None:
    """Bring the ambient container up, creating it only if it is absent."""
    state = _container_state()
    if state == "running":
        return
    if state:
        _docker("start", CONTAINER_NAME)
        return
    proc = _docker(
        "run",
        "-d",
        "--name",
        CONTAINER_NAME,
        "-e",
        f"MINIO_ROOT_USER={ACCESS_KEY}",
        "-e",
        f"MINIO_ROOT_PASSWORD={SECRET_KEY}",
        "-p",
        f"127.0.0.1:{HOST_PORT}:9000",
        CONTAINER_IMAGE,
        "server",
        "/data",
    )
    if proc.returncode != 0:
        # A racing worker may have created it a moment ago. Existing in any
        # state is enough -- the caller polls for readiness.
        if _container_state():
            _docker("start", CONTAINER_NAME)
            return
        raise ObjectStorageUnavailable(
            f"could not start {CONTAINER_NAME}: {proc.stderr.strip()}"
        )


def _await_ready(endpoint: str, *, attempts: int = 60) -> None:
    """Poll MinIO's documented liveness probe until it answers."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed localhost URL
                f"{endpoint}/minio/health/live", timeout=5
            ) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:  # pragma: no cover - timing
            last = exc
        time.sleep(0.5)
    raise ObjectStorageUnavailable(f"minio at {endpoint} never came up: {last}")


def _ensure_bucket(endpoint: str) -> None:
    """Create the agents bucket if it is absent.

    Uses the S3 API through aioboto3 -- already a runtime dependency -- so
    the harness does not need the separate ``minio/mc`` image the CI
    workflow shells out to.
    """
    import asyncio

    import aioboto3
    from botocore.exceptions import ClientError

    async def _create() -> None:
        session = aioboto3.Session(
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
            region_name="us-east-1",
        )
        async with session.client("s3", endpoint_url=endpoint) as client:
            try:
                await client.head_bucket(Bucket=BUCKET)
                return
            except ClientError:
                pass
            with contextlib.suppress(ClientError):
                # A racing worker may have won; an already-owned bucket is
                # success, not failure.
                await client.create_bucket(Bucket=BUCKET)

    asyncio.run(_create())


def resolve_storage_env() -> dict[str, str]:
    """Resolve, and if necessary provision, an object store.

    Returns the ``STORAGE_*`` mapping naming it, ready to be exported into
    ``os.environ`` for the integration files that build their config from
    the environment.

    An explicit ``STORAGE_ENDPOINT_URL`` short-circuits Docker entirely.
    That is the CI path, where a container already exists, and it is also
    the escape hatch for a developer pointing the suite at their own store.
    """
    explicit = os.environ.get("STORAGE_ENDPOINT_URL")
    if explicit and explicit != _default_endpoint():
        return {}

    endpoint = os.environ.get("DITTO_TEST_MINIO_ENDPOINT") or _default_endpoint()
    try:
        _await_ready(endpoint, attempts=1)
    except ObjectStorageUnavailable:
        with _startup_lock():
            # Re-check under the lock: by the time we got it, the worker
            # that held it first has usually finished the whole job.
            try:
                _await_ready(endpoint, attempts=1)
            except ObjectStorageUnavailable:
                _start_container()
                _await_ready(endpoint)
    _ensure_bucket(endpoint)
    return {
        "STORAGE_ENDPOINT_URL": endpoint,
        "STORAGE_BUCKET": BUCKET,
        "STORAGE_ACCESS_KEY": ACCESS_KEY,
        "STORAGE_SECRET_KEY": SECRET_KEY,
        "STORAGE_REGION": "us-east-1",
        "STORAGE_USE_TLS": "false",
    }


def _default_endpoint() -> str:
    return f"http://127.0.0.1:{HOST_PORT}"
