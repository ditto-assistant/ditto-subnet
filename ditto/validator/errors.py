"""Typed errors for the validator worker."""

from __future__ import annotations


class ValidatorError(Exception):
    """Base class for validator-worker failures."""


class ValidatorConfigError(ValidatorError):
    """Raised when the worker is misconfigured (missing/invalid env)."""


class DittobenchError(ValidatorError):
    """Raised when a dittobench-api run fails or times out."""


class PlatformError(ValidatorError):
    """Raised when a platform ``/validator/*`` call fails."""
