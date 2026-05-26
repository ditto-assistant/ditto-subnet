"""Starlette middleware + FastAPI exception handlers."""

from __future__ import annotations

from ditto.api_server.middleware.auth_pass_through import AuthPassThroughMiddleware
from ditto.api_server.middleware.error_envelope import register_exception_handlers
from ditto.api_server.middleware.request_id import (
    REQUEST_ID_HEADER,
    RequestIdFilter,
    RequestIDMiddleware,
    request_id_var,
)

__all__ = [
    "AuthPassThroughMiddleware",
    "REQUEST_ID_HEADER",
    "RequestIdFilter",
    "RequestIDMiddleware",
    "register_exception_handlers",
    "request_id_var",
]
