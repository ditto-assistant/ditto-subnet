"""Fresh JSON-RPC header reads without initializing a SCALE runtime decoder."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


@asynccontextmanager
async def header_substrate(url: str) -> AsyncIterator[Any]:
    """Own one transport for hash/header helpers; never use it for storage.

    The SDK's normal context entry also launches metadata initialization. These
    JSON-RPC methods need no runtime, and decoding it competes with API work.
    Keep SDK request/error handling and close the connection even when opening
    or reading it fails. Each invocation has fresh transport and block caches.
    """
    from async_substrate_interface import AsyncSubstrateInterface

    substrate = AsyncSubstrateInterface(url=url)
    try:
        await substrate.ws.connect()
        yield substrate
    finally:
        await substrate.close()
