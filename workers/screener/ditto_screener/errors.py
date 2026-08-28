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
