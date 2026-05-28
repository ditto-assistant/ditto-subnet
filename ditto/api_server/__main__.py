"""Process entry point for the API server.

Resolves config (argparse + env), configures stdlib logging, builds the
FastAPI app via :func:`create_api_server`, and hands it to uvicorn.
Uncaught startup failures land in the crash path, log a traceback, and
exit non-zero so process supervisors restart cleanly.
"""

from __future__ import annotations

import argparse
import logging
import logging.config
import os
import subprocess
import sys
from dataclasses import replace

import uvicorn

from ditto.api_server.config import (
    ApiServerConfig,
    check_config,
    parse_api_server_config_from_env,
)
from ditto.api_server.errors import ApiServerConfigError
from ditto.api_server.factory import create_api_server
from ditto.api_server.logging_config import build_dict_config

logger = logging.getLogger(__name__)


def add_args(parser: argparse.ArgumentParser) -> None:
    """Register API-level CLI flags. Sub-configs come from env."""
    parser.add_argument(
        "--host",
        type=str,
        default=os.environ.get("API_HOST", "0.0.0.0"),
        help="Interface to bind. Defaults to 0.0.0.0 / $API_HOST.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("API_PORT", "8000")),
        help="TCP port. Defaults to 8000 / $API_PORT.",
    )
    parser.add_argument(
        "--log-level",
        dest="log_level",
        type=str,
        default=os.environ.get("API_LOG_LEVEL", "INFO"),
        help="Root logger level. Defaults to INFO / $API_LOG_LEVEL.",
    )


def _resolve_commit_hash() -> str:
    """Return the git revision the process was built from.

    Falls back to ``"unknown"`` on any failure (subprocess error, non-zero
    exit, missing git binary, no ``.git`` directory). The fallback lets
    deploy images without git history boot cleanly.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip() or "unknown"


def _config_from_args(ns: argparse.Namespace) -> ApiServerConfig:
    """Resolve env-driven sub-configs, then overlay argparse top-level values."""
    commit = _resolve_commit_hash()
    base = parse_api_server_config_from_env(commit_hash=commit)
    return replace(
        base,
        host=ns.host,
        port=ns.port,
        log_level=ns.log_level.upper(),
    )


def _redact(value: str | None, keep: int = 4) -> str:
    """Mask all but the last ``keep`` chars of a sensitive string."""
    if not value:
        return "<unset>"
    if len(value) <= keep:
        return "***"
    return f"***{value[-keep:]}"


def _config_to_log_dict(config: ApiServerConfig) -> dict[str, object]:
    """Build a redacted JSON-safe view of the resolved config for boot logging."""
    return {
        "api": {
            "host": config.host,
            "port": config.port,
            "log_level": config.log_level,
            "commit": config.commit_hash,
        },
        "postgres": {
            "host": config.postgres.host,
            "port": config.postgres.port,
            "user": config.postgres.user,
            "password": _redact(config.postgres.password),
            "database": config.postgres.database,
            "pool_min_size": config.postgres.pool_min_size,
            "pool_max_size": config.postgres.pool_max_size,
            "command_timeout": config.postgres.command_timeout,
        },
        "chain": {
            "pylon_url": config.chain.pylon_url,
            "netuid": config.chain.netuid,
            "subtensor_network": config.chain.subtensor_network,
            "open_access_token": _redact(config.chain.open_access_token),
            "identity_name": config.chain.identity_name or "<unset>",
            "identity_token": _redact(config.chain.identity_token),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ditto.api_server")
    add_args(parser)
    ns = parser.parse_args(argv)

    try:
        config = _config_from_args(ns)
        check_config(config)
    except ApiServerConfigError as e:
        # Logging is not configured yet; write directly to stderr so the
        # supervisor sees the cause.
        sys.stderr.write(f"api server config error: {e}\n")
        return 2

    logging.config.dictConfig(build_dict_config(config.log_level))
    logger.info(f"api server starting: {_config_to_log_dict(config)}")

    try:
        uvicorn.run(
            create_api_server(config),
            host=config.host,
            port=config.port,
            log_config=None,
            server_header=False,
            date_header=False,
            timeout_graceful_shutdown=30,
        )
    except Exception:
        logger.exception("api server crashed")
        os._exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
