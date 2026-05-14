"""Errors raised by the chain access layer."""

from __future__ import annotations


class ChainError(Exception):
    """Base exception for chain-related errors."""

    pass


# --- Connection errors ---


class ChainConnectionError(ChainError):
    """Raised when a connection to Pylon or the underlying chain fails.

    This can happen when:
    - The Pylon service is not running or not reachable at ``pylon_url``.
    - The host has lost network connectivity to Pylon.
    - Pylon authentication failed (wrong ``identity_name`` / ``identity_token``).
    - Pylon's underlying subtensor node is unreachable from the Pylon container.
    - The configured ``subtensor_network`` is wrong or unreachable.
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
