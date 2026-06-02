"""Resolved configuration for the API server process."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from ditto.api_server.errors import ApiServerConfigError
from ditto.api_server.pricing import PricingConfig, parse_pricing_config_from_env
from ditto.api_server.storage import StorageConfig, parse_storage_config_from_env
from ditto.chain import ChainConfig, parse_chain_config_from_env
from ditto.db import PostgresConfig, parse_postgres_config_from_env

# Substrate SS58 base58 alphabet, 47-48 chars. Same shape Pydantic
# enforces on the wire; mirrored here so a bad payment address fails
# boot instead of running with a placeholder.
_SS58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")


@dataclass(frozen=True)
class ApiServerConfig:
    """Resolved configuration for the API server process.

    Composition over flattening: ``postgres``, ``chain``, and ``pricing``
    carry their own typed dataclasses so the same configs feed validator
    daemon + smoke scripts unchanged.
    """

    host: str
    """Interface to bind. ``0.0.0.0`` for compose / cloud, ``127.0.0.1`` locally."""

    port: int
    """TCP port. Defaults to 8000; Pylon shifts to 8001 in compose."""

    log_level: str
    """Root logger level. One of the stdlib level names (``DEBUG``, ``INFO``,
    ``WARNING``, ``ERROR``, ``CRITICAL``)."""

    commit_hash: str
    """Git revision the process was built from, or ``"unknown"`` outside a checkout.

    Resolved by :mod:`ditto.api_server.__main__` via ``git rev-parse HEAD``
    before the FastAPI app is built, so :func:`create_api_server` can stash
    it on ``app.state.commit_hash`` for the ``/health`` endpoint.
    """

    upload_payment_address: str
    """Ditto-controlled SS58 receive address for upload fees
    (``DITTO_UPLOAD_PAYMENT_ADDRESS``). Required at boot."""

    postgres: PostgresConfig
    """Connection parameters for the platform database."""

    chain: ChainConfig
    """Pylon + subtensor settings for chain reads."""

    pricing: PricingConfig
    """CoinGecko oracle + upload-fee parameters."""

    storage: StorageConfig
    """S3-compatible object store parameters for uploaded tarballs."""


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def parse_api_server_config_from_env(commit_hash: str) -> ApiServerConfig:
    """Build an :class:`ApiServerConfig` from ``API_*`` env vars plus
    the postgres + chain + pricing sub-config parsers. Call
    :func:`check_config` after to validate ranges + set membership.

    Raises:
        ApiServerConfigError: When ``API_PORT`` is not parseable as int
            or ``DITTO_UPLOAD_PAYMENT_ADDRESS`` is unset.
    """
    host = os.environ.get("API_HOST", "0.0.0.0")
    raw_port = os.environ.get("API_PORT", "8000")
    log_level = os.environ.get("API_LOG_LEVEL", "INFO").upper()

    try:
        port = int(raw_port)
    except ValueError as e:
        raise ApiServerConfigError(
            f"API_PORT must be an integer, got {raw_port!r}"
        ) from e

    upload_payment_address = os.environ.get("DITTO_UPLOAD_PAYMENT_ADDRESS")
    if not upload_payment_address:
        raise ApiServerConfigError(
            "DITTO_UPLOAD_PAYMENT_ADDRESS must be set to a Ditto-controlled "
            "SS58 receive address"
        )
    if not _SS58_RE.fullmatch(upload_payment_address):
        raise ApiServerConfigError(
            "DITTO_UPLOAD_PAYMENT_ADDRESS does not look like an SS58 address; "
            "got a value that fails the base58 / length check"
        )

    return ApiServerConfig(
        host=host,
        port=port,
        log_level=log_level,
        commit_hash=commit_hash,
        upload_payment_address=upload_payment_address,
        postgres=parse_postgres_config_from_env(),
        chain=parse_chain_config_from_env(),
        pricing=parse_pricing_config_from_env(),
        storage=parse_storage_config_from_env(),
    )


def check_config(config: ApiServerConfig) -> None:
    """Validate port range + log-level set membership.

    Raises:
        ApiServerConfigError: When ``port`` is outside ``1..65535`` or
            ``log_level`` is not a stdlib level name.
    """
    if not 1 <= config.port <= 65535:
        raise ApiServerConfigError(f"port out of range: {config.port}")
    if config.log_level not in _VALID_LOG_LEVELS:
        raise ApiServerConfigError(
            f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}; "
            f"got {config.log_level!r}"
        )
