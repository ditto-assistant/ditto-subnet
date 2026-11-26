"""Fixed protocol bounds for edge/v1.

These are wire limits every implementation enforces identically. Operator
tunables (replica count, lease length, challenge rate) live in Platform's lane
settings and reach nodes as :class:`~ditto_edge_protocol.wire.LaneLimits`;
they must stay inside these bounds.
"""

from __future__ import annotations

from typing import Final

PROTOCOL: Final = "edge/v1"

MIB: Final = 1024 * 1024
GIB: Final = 1024 * MIB

MAX_OBJECT_BYTES_CEILING: Final = 50 * MIB
"""Largest object the protocol allows, matching the share service's 50 MiB
attachment cap. Lane settings choose a smaller working limit."""

MAX_POSSESSION_RANGE: Final = 64 * 1024
"""Longest byte range a possession challenge may hash."""

MAX_RETRIEVAL_RANGE: Final = 256 * 1024
"""Longest byte range a retrieval challenge may return."""

REQUEST_SKEW_SECONDS: Final = 120
"""How far a signed request's timestamp may sit from the server clock."""

NONCE_BYTES: Final = 16
"""Random bytes in a request nonce (hex-encoded on the wire)."""

CHALLENGE_NONCE_BYTES: Final = 32
"""Random bytes keying a possession proof (hex-encoded on the wire)."""

MAX_ACK_ITEMS: Final = 512
"""Digests one acknowledgement may carry in each list."""

MAX_ASSIGNMENTS_PER_POLL: Final = 1024
"""Assignments one poll response may list."""

MAX_CHALLENGES_PER_POLL: Final = 128
"""Challenges one poll response may carry; a window holds at most 120 slots."""

MEDIA_TYPES: Final = frozenset(
    {
        "application/json",
        "application/octet-stream",
        "application/pdf",
        "font/woff2",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/css",
        "text/csv",
        "text/html",
        "text/javascript",
        "text/markdown",
        "text/plain",
    }
)
"""Media types a node may be asked to hold. Nodes never render or execute
what they hold; the type only travels with the bytes so a future edge can serve
them with the right headers."""
