"""Typed errors for the screener worker.

Mirrors :mod:`ditto.validator.errors`: a config error fails the process fast at
boot; a platform error aborts the current attempt without killing the daemon.
"""

from __future__ import annotations


class ScreenerConfigError(Exception):
    """Raised at boot when the env-driven config is missing/invalid.

    Fatal: the process exits rather than run with a placeholder (e.g. no signing
    key, no platform URL).
    """


class PlatformError(Exception):
    """A ``/screener/*`` HTTP call failed or returned a non-2xx status.

    The worker logs it and moves on. Platform parks the failed attempt until a
    Backroom operator explicitly authorizes another attempt. Never process-fatal.
    """


class PlatformRejected(PlatformError):
    """Platform definitively refused a verdict, with bounded diagnostics."""

    def __init__(self, *, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body[:500]
        super().__init__(f"verdict rejected ({status_code}): {self.body}")


class PlatformAuthUnavailable(PlatformError):
    """Permanent local authentication failure that retries cannot repair."""


class PlatformAuthOnlyFailure(PlatformError):
    """Authentication failed before any verdict request was dispatched."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"{detail}; no verdict request was sent")
