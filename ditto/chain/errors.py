"""Errors raised by the chain access layer."""

from __future__ import annotations


class ChainError(Exception):
    """Base exception for chain-related errors."""

    pass


# --- Auth errors ---


class ChainAuthError(ChainError):
    """Raised when Pylon rejects a request for auth-related reasons.

    Distinct from :class:`ChainConnectionError` so callers can react to
    auth failures specifically (rotate credentials, refuse to start, flag
    misconfiguration) without confusing them with transient network issues.

    This can happen when:
    - ``open_access_token`` is missing, empty, or wrong (Pylon returns 401).
    - ``identity_name`` / ``identity_token`` are missing or wrong when an
      identity-mode endpoint is called (Pylon returns 401).
    - The configured identity exists but is forbidden from the requested
      operation (Pylon returns 403). Most common case: validator daemon
      calling ``put_weights`` without the required validator permit or stake.
    - ``ChainConfig`` was loaded with neither auth mode set and somehow
      reached an identity-mode call path.
    """

    pass


# --- Connection errors ---


class ChainConnectionError(ChainError):
    """Raised when a connection to Pylon or the underlying chain fails.

    Auth-specific failures live in :class:`ChainAuthError`; this class covers
    transport-layer and infrastructure-layer faults only.

    This can happen when:
    - The Pylon service is not running or not reachable at ``pylon_url``.
    - The host has lost network connectivity to Pylon.
    - Pylon's underlying subtensor node is unreachable from the Pylon container.
    - The configured ``subtensor_network`` is wrong or unreachable.
    - Pylon returned a 5xx or an otherwise-unexpected error that is not a
      timeout, not a 404, and not an auth rejection.
    """

    pass


# --- Lookup errors ---


class ExtrinsicNotFoundError(ChainError):
    """Raised when an extrinsic at the requested block + index does not exist.

    This can happen when:
    - The block number is past the chain head.
    - The extrinsic index is beyond the count of extrinsics in the block.
    - The block has not yet been finalized and the archive node has not caught up.
    - The block hash supplied for a lookup does not match what Pylon has indexed.
    - No matching ``ExtrinsicSuccess`` / ``ExtrinsicFailed`` event was emitted for
      the extrinsic index at the requested block.
    """

    pass


# --- Timeout errors ---


class ChainTimeoutError(ChainError):
    """Raised when a chain request exceeded its allotted timeout.

    This can happen when:
    - Pylon is overloaded and slow to respond.
    - The underlying subtensor node is under load and stalling event reads.
    - A network hiccup between this host and Pylon delayed the response.
    - async-substrate-interface failed to receive a WebSocket frame in time.
    """

    pass
