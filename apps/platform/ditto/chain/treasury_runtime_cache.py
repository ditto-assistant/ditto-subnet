"""Bounded decoded metadata reuse; never caches a chain storage observation."""

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any


class TreasuryRuntimeCache:
    """Single-flight metadata loads scoped to one ChainClient/event loop.

    The SDK already reuses Runtime objects by specVersion within a connection.
    Extend only that decoded type metadata lifetime across fresh connections,
    isolating endpoint and genesis and retaining at most four runtime versions.
    Cancellation or a failed load releases the lock without caching a result.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._values: OrderedDict[tuple[str, str, int], Any] = OrderedDict()

    async def get(
        self,
        url: str,
        genesis: str,
        version: int,
        load: Callable[[], Awaitable[Any]],
    ) -> Any:
        key = (url, genesis, version)
        async with self._lock:
            if key in self._values:
                self._values.move_to_end(key)
                return self._values[key]
            runtime = await load()
            if runtime.runtime_version != version:
                raise ValueError("treasury runtime metadata version mismatch")
            self._values[key] = runtime
            if len(self._values) > 4:
                self._values.popitem(last=False)
            return runtime
