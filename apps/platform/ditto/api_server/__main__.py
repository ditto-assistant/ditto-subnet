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
import sys
from dataclasses import replace

import uvicorn

from ditto.api_server import revision
from ditto.api_server.config import (
    ApiServerConfig,
    check_config,
    parse_api_server_config_from_env,
)
from ditto.api_server.errors import ApiServerConfigError
from ditto.api_server.factory import create_api_server
from ditto.api_server.logging_config import build_dict_config

logger = logging.getLogger(__name__)

# Self-hosted dev subtensor node. ``--dev`` points the API's chain reads
# here instead of finney. NOTE: the API also reads the chain through Pylon;
# whichever node ``PYLON_URL`` targets must be pointed at this same endpoint
# for full dev-mode chain access. The flag only controls the in-process
# substrate-interface reader (the "Pylon gap" event reads).
DEV_SUBTENSOR_ENDPOINT = "ws://68.183.141.180:80"


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
    parser.add_argument(
        "--dev",
        action="store_true",
        default=os.environ.get("DITTO_ENV", "").lower() == "dev",
        help=(
            "Dev mode: point chain reads at the self-hosted dev subtensor "
            f"({DEV_SUBTENSOR_ENDPOINT}) instead of finney. Also enabled by "
            "DITTO_ENV=dev. Pylon must target the same endpoint."
        ),
    )


def _resolve_commit_hash() -> str:
    """Return the git revision the process is being built from.

    Resolved exactly once, here at startup, and then carried for the life of
    the process: it is the record of what this process is actually running,
    which is why ``/health`` can compare it against the checkout that a later
    deploy may have moved underneath it. See :mod:`ditto.api_server.revision`.
    """
    return revision.resolve_commit_hash()


def _config_from_args(ns: argparse.Namespace) -> ApiServerConfig:
    """Resolve env-driven sub-configs, then overlay argparse top-level values."""
    commit = _resolve_commit_hash()
    base = parse_api_server_config_from_env(commit_hash=commit)
    chain = base.chain
    if getattr(ns, "dev", False):
        # Dev mode overrides the subtensor network the in-process reader
        # uses; Pylon's own target is configured wherever Pylon runs.
        chain = replace(chain, subtensor_network=DEV_SUBTENSOR_ENDPOINT)
    return replace(
        base,
        host=ns.host,
        port=ns.port,
        log_level=ns.log_level.upper(),
        chain=chain,
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
        "upload": {
            "payment_address": config.upload_payment_address,
        },
        "screener": {
            "enabled": config.screener_auth.enabled,
            "hotkey": config.screener_auth.hotkey or "<unset>",
            "api_token": _redact(config.screener_auth.api_token),
        },
        "pricing": {
            "fee_usd": str(config.pricing.fee_usd),
            "fee_buffer": str(config.pricing.fee_buffer),
            "cache_ttl_seconds": config.pricing.cache_ttl_seconds,
            "max_stale_seconds": config.pricing.max_stale_seconds,
            "coingecko_timeout_seconds": config.pricing.coingecko_timeout_seconds,
            "override_tao_usd": (
                str(config.pricing.override_tao_usd)
                if config.pricing.override_tao_usd is not None
                else "<unset>"
            ),
        },
        "validator_names": {
            "enabled": config.validator_names.enabled,
            "timeout_seconds": config.validator_names.timeout_seconds,
            "refresh_seconds": config.validator_names.refresh_seconds,
            "retry_seconds": config.validator_names.retry_seconds,
            "max_stale_seconds": config.validator_names.max_stale_seconds,
        },
        "storage": {
            "endpoint_url": config.storage.endpoint_url,
            "bucket": config.storage.bucket,
            "region": config.storage.region,
            "use_tls": config.storage.use_tls,
            "access_key": _redact(config.storage.access_key),
            "secret_key": _redact(config.storage.secret_key),
        },
        "embedding": {
            "enabled": config.embedding.enabled,
            "url": config.embedding.url or "<unset>",
            "model": config.embedding.model or "<unset>",
            "revision": config.embedding.revision,
            "dim": (
                config.embedding.dim if config.embedding.dim is not None else "native"
            ),
            "timeout_seconds": config.embedding.timeout_seconds,
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
            # Keep the Date header: responses carry Cache-Control max-age, and a
            # browser needs Date to compute the response's current age and honor
            # freshness. Only the Server banner is suppressed.
            date_header=True,
            timeout_graceful_shutdown=30,
        )
    except Exception:
        logger.exception("api server crashed")
        os._exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
