"""Typed errors for the screener worker.

Mirrors :mod:`ditto.validator.errors`: a config error fails the process fast at
boot; a platform error aborts the current attempt without killing the daemon.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import ParamSpec, TypeVar

_P = ParamSpec("_P")
_R = TypeVar("_R")


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
    """A non-transient verdict response, with bounded diagnostics.

    A conflict can follow an earlier accepted dispatch; the status alone does
    not prove that replacing the original verdict is safe.
    """

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


def model_output_contract(
    message: str,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Report a reviewer value of the wrong type as an ordinary contract miss.

    Reviewer tool schemas are not strict, so a model can send a list or dict
    where a string is expected. A membership test against a set then raises
    ``TypeError: unhashable type``, which the review paths do not treat as a
    malformed reply: it escaped to the gate's catch-all and parked the review
    as ``unexpected-infrastructure``, losing its notes (ditto-subnet#2987).
    Applied only to functions that parse model output, so the ``ValueError``
    takes the same repair-turn and contract-miss paths as any other invalid
    field.
    """

    def decorate(parse: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(parse)
        def checked(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            try:
                return parse(*args, **kwargs)
            except TypeError as error:
                raise ValueError(message) from error

        return checked

    return decorate
