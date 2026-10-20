"""Bounded decoded metadata reuse; never caches a chain storage observation."""

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ditto.chain.errors import ChainConnectionError


@dataclass
class _RuntimeLoad:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class TreasuryRuntimeCache:
    """Single-flight metadata loads scoped to one ChainClient/event loop.

    The SDK already reuses Runtime objects by specVersion within a connection.
    Extend only that decoded type metadata lifetime across fresh connections,
    isolating endpoint and genesis and retaining at most four runtime versions.
    Cancellation or a failed load releases the lock without caching a result.
    """

    def __init__(self) -> None:
        self._values: OrderedDict[tuple[str, str, int], Any] = OrderedDict()
        self._loads: dict[tuple[str, str, int], _RuntimeLoad] = {}

    async def get(
        self,
        url: str,
        genesis: str,
        version: int,
        load: Callable[[], Awaitable[Any]],
    ) -> Any:
        key = (url, genesis, version)
        # No await occurs here: event-loop-local hits and their LRU update
        # cannot interleave, and never wait behind another key's network load.
        if key in self._values:
            self._values.move_to_end(key)
            return self._values[key]
        pending = self._loads.setdefault(key, _RuntimeLoad())
        pending.users += 1
        try:
            async with pending.lock:
                if key in self._values:
                    self._values.move_to_end(key)
                    return self._values[key]
                runtime = await load()
                if runtime.runtime_version != version:
                    raise ChainConnectionError(
                        "treasury runtime metadata version mismatch"
                    )
                self._values[key] = runtime
                if len(self._values) > 4:
                    self._values.popitem(last=False)
                return runtime
        finally:
            # Include waiters so cancellation never removes a lock still used
            # by another caller. No detached task survives its read deadline.
            pending.users -= 1
            if not pending.users:
                del self._loads[key]
