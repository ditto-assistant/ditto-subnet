"""Immutable public source-control artifact packaged from v0.330.0."""

from __future__ import annotations

import hashlib
from importlib import resources

RELEASE = "v0.330.0"
RELEASE_COMMIT = "7dafedb87125b4a22104d0903997c9f36976be3d"
SOURCE_TREE = "f13fe9215f531819d6e94c099b25eaa7545b715d"
ARCHIVE_SHA256 = "a3dacec019ce5ea6694bfbeb7669a5f9109514c38c6de7000c8dfa3f3f0f57b6"
ARCHIVE_BYTES = 4_914_769
DOCKERFILE_SHA256 = "d3a1a2a1e5d43b0465c28712457d95432942ac8f017fd10d538859a901a54641"
OBJECT_KEY = "source-controls/canonical-starter-v0.330.0.tgz"


def archive_bytes() -> bytes:
    data = (
        resources.files("ditto.api_server")
        .joinpath("data/canonical-starter-v0.330.0.tgz")
        .read_bytes()
    )
    if len(data) != ARCHIVE_BYTES or hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError("packaged canonical starter control does not match release")
    return data
