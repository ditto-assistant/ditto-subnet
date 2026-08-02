"""HTTP-side glue for the served-artifact audit trail.

Keeps the request-shaped concerns (peer address, forwarded-for context) out of
:mod:`ditto.db.queries.artifact_fetch_audit`, which stays a pure DB helper.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import Request

# Caller-supplied forwarding headers, recorded as untrusted context only.
_FORWARD_HEADERS = ("x-forwarded-for", "x-real-ip")


def client_ip(request: Request | None) -> str | None:
    """Return the transport peer address, or ``None`` when there isn't one.

    Deliberately the *peer* address rather than anything out of
    ``X-Forwarded-For``: forwarding headers are attacker-controlled on any
    request that reaches the app directly, so treating them as identity would
    let a leaker write whatever origin they liked into the audit trail. Behind
    a proxy the peer is the proxy, and the claimed client chain is preserved
    separately by :func:`forwarded_context` as *data*, not as identity.
    """
    client = getattr(request, "client", None) if request is not None else None
    host = getattr(client, "host", None)
    return host or None


def forwarded_context(request: Request | None) -> dict[str, Any] | None:
    """Return claimed forwarding headers for the audit ``detail`` blob.

    Untrusted by construction — a reader correlating an incident should treat
    these as a claim to corroborate, never as the source of truth that
    :func:`client_ip` provides.
    """
    if request is None:
        return None
    claimed = {
        header: value
        for header in _FORWARD_HEADERS
        if (value := request.headers.get(header))
    }
    return {"claimed_forwarded_for": claimed} if claimed else None


def request_detail(request: Request | None, **extra: Any) -> dict[str, Any] | None:
    """Merge route-specific audit context with the untrusted forwarding claim."""
    detail: dict[str, Any] = {k: v for k, v in extra.items() if v is not None}
    forwarded = forwarded_context(request)
    if forwarded is not None:
        detail.update(forwarded)
    return detail or None
