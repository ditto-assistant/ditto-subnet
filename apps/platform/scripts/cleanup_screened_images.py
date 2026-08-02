#!/usr/bin/env python3
"""Run one eligibility-aware screened-image cleanup pass."""

from __future__ import annotations

import asyncio
import logging

from ditto.api_server.config import parse_api_server_config_from_env
from ditto.api_server.screened_image_cleanup import cleanup_screened_images
from ditto.api_server.storage import create_storage_client
from ditto.db import create_db_engine, create_session_maker


async def _main() -> None:
    config = parse_api_server_config_from_env("screened-image-cleanup")
    engine = create_db_engine(config.postgres)
    try:
        async with create_storage_client(config.storage) as storage:
            result = await cleanup_screened_images(
                create_session_maker(engine), storage
            )
            logging.info("screened image cleanup complete: %s", result)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
