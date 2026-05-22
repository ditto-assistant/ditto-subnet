"""Stdlib :mod:`logging` ``dictConfig`` builder for the API server.

The practices doc forbids custom logger modules; this file is allowed
because it returns a data dict, not a wrapped ``Logger`` object. The
:class:`logging.Filter` lives in :mod:`ditto.api_server.middleware.request_id`
so a single dotted import path covers it.

Configures:
- One stdout handler with the request-id filter attached
- One formatter that includes ``%(request_id)s`` so every log line in a
  request's task scope carries the correlation id
- ``uvicorn`` + ``uvicorn.error`` routed into the same handler
- ``uvicorn.access`` disabled (the request-id middleware emits the
  access log line so the field is filled in)
"""

from __future__ import annotations

from typing import Any


def build_dict_config(level: str) -> dict[str, Any]:
    """Return a :func:`logging.config.dictConfig` payload for the API server.

    Args:
        level: Root logger level (already validated by
            :func:`ditto.api_server.config.parse_api_server_config_from_env`).
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "request_id": {
                "()": "ditto.api_server.middleware.request_id.RequestIdFilter",
            },
        },
        "formatters": {
            "default": {
                "format": (
                    "%(asctime)s %(levelname)s [%(name)s] [%(request_id)s] %(message)s"
                ),
            },
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "default",
                "filters": ["request_id"],
            },
        },
        "loggers": {
            "uvicorn": {
                "handlers": ["stdout"],
                "level": level,
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["stdout"],
                "level": level,
                "propagate": False,
            },
            # Disabled: the request_id middleware emits its own access log
            # line so the field is filled in. Letting uvicorn.access through
            # double-logs every request.
            "uvicorn.access": {
                "handlers": [],
                "level": "WARNING",
                "propagate": False,
            },
        },
        "root": {
            "handlers": ["stdout"],
            "level": level,
        },
    }
