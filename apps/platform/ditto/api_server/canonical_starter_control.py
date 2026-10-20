"""Immutable public source-control artifact packaged from v0.330.5."""

from __future__ import annotations

import hashlib
from importlib import resources

RELEASE = "v0.330.5"
RELEASE_COMMIT = "940304019aeec55e7b473bc163a51851e99db907"
SOURCE_TREE = "9ffd5370e21bbe3135f1ee830b7b68723950619b"
ARCHIVE_SHA256 = "2f14f77cc8e21b57e96f304f3b621d9919e9af802076928a27301d57aa956d7e"
ARCHIVE_BYTES = 4_915_701
DOCKERFILE_SHA256 = "d3a1a2a1e5d43b0465c28712457d95432942ac8f017fd10d538859a901a54641"
OBJECT_KEY = "source-controls/canonical-starter-v0.330.5.tgz"


def archive_bytes() -> bytes:
    data = (
        resources.files("ditto.api_server")
        .joinpath("data/canonical-starter-v0.330.5.tgz")
        .read_bytes()
    )
    if len(data) != ARCHIVE_BYTES or hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError("packaged canonical starter control does not match release")
    return data
