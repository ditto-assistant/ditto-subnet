"""Authenticated platform preflight used by container and deploy health checks."""

from __future__ import annotations

import asyncio
import os

import httpx

from ditto_screener.config import parse_screener_config_from_env
from ditto_screener.platform import PlatformClient
from ditto_screening_protocol import (
    SCREENING_FLOOR_POLICY_VERSION,
    SCREENING_POLICY_VERSION,
)


async def _check() -> None:
    # This trusted image also serves a one-shot source-review command. Its
    # success boundary is the attempt-bound completion POST, so the long-lived
    # worker preflight does not apply to that explicitly selected mode.
    if os.environ.get("DITTO_SOURCE_REVIEW_JOB") == "1":
        return
    config = parse_screener_config_from_env()
    async with httpx.AsyncClient(timeout=config.http_timeout_seconds) as http:
        required = await PlatformClient(config, http).get_required_policy_version()
    if not (SCREENING_FLOOR_POLICY_VERSION <= required <= SCREENING_POLICY_VERSION):
        raise RuntimeError(
            f"platform requires policy {required}; worker supports "
            f"{SCREENING_FLOOR_POLICY_VERSION}-{SCREENING_POLICY_VERSION}"
        )


def main() -> None:
    asyncio.run(_check())


if __name__ == "__main__":
    main()
