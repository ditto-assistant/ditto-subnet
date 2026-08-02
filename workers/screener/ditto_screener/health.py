"""Authenticated platform preflight used by container and deploy health checks."""

from __future__ import annotations

import asyncio

import httpx

from ditto_screener.config import parse_screener_config_from_env
from ditto_screener.platform import PlatformClient
from ditto_screening_protocol import SCREENING_POLICY_VERSION


async def _check() -> None:
    config = parse_screener_config_from_env()
    async with httpx.AsyncClient(timeout=config.http_timeout_seconds) as http:
        required = await PlatformClient(config, http).get_required_policy_version()
    if required != SCREENING_POLICY_VERSION:
        raise RuntimeError(
            f"platform requires policy {required}; worker supports "
            f"{SCREENING_POLICY_VERSION}"
        )


def main() -> None:
    asyncio.run(_check())


if __name__ == "__main__":
    main()
