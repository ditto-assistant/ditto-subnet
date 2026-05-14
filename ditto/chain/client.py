"""ChainClient — async context manager wrapping Pylon and substrate-interface."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ditto.chain.errors import (
    ChainConnectionError,
    ChainError,
    ChainTimeoutError,
    ExtrinsicNotFoundError,
)
from ditto.chain.models import BlockInfo, ChainConfig, ExtrinsicInfo, NeuronInfo

if TYPE_CHECKING:
    from types import TracebackType

    from pylon_client import AsyncPylonClient

logger = logging.getLogger(__name__)


# --- Substrate WebSocket URLs (used only for the Pylon events gap) ---

_FINNEY_WS_URL = "wss://entrypoint-finney.opentensor.ai:443"
_TEST_WS_URL = "wss://test.finney.opentensor.ai:443"
_LOCAL_WS_URL = "ws://127.0.0.1:9944"

# --- substrate ``System.Events`` identifiers we filter on ---

_SYSTEM_MODULE = "System"
_EXTRINSIC_SUCCESS_EVENT = "ExtrinsicSuccess"
_EXTRINSIC_FAILED_EVENT = "ExtrinsicFailed"


class ChainClient:
    """Async context manager wrapping Pylon for chain access.

    Holds an :class:`AsyncPylonClient` for the duration of the ``async with``
    block. Methods cover the chain interactions Ditto's validator and platform
    need: neuron discovery, block + extrinsic reads, weight setting. Extrinsic
    success / failure detection — which Pylon does not surface — is layered on
    via a small ``async-substrate-interface`` event read inside
    :meth:`check_extrinsic_success`.

    Usage:
        async with ChainClient(config) as client:
            block = await client.get_latest_block()
            ext = await client.get_extrinsic(block.number, 0)
            if ext.succeeded:
                ...
    """

    def __init__(self, config: ChainConfig) -> None:
        """Store the config; the underlying Pylon client is built in ``__aenter__``."""
        self._config = config
        self._pylon: AsyncPylonClient | None = None

    async def __aenter__(self) -> ChainClient:
        """Open the underlying Pylon client connection."""
        from pylon_client import AsyncPylonClient

        try:
            self._pylon = AsyncPylonClient(
                url=self._config.pylon_url,
                identity_name=self._config.identity_name,
                identity_token=self._config.identity_token,
            )
            await self._pylon.__aenter__()
        except Exception as e:
            raise ChainConnectionError(
                f"failed to connect to Pylon at {self._config.pylon_url}: {e}"
            ) from e
        logger.info(
            f"ChainClient connected to Pylon at {self._config.pylon_url} "
            f"(netuid={self._config.netuid})"
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the underlying Pylon client."""
        if self._pylon is not None:
            await self._pylon.__aexit__(exc_type, exc, tb)
            self._pylon = None

    def _ensure_pylon(self) -> AsyncPylonClient:
        """Return the active Pylon client; raise if used outside ``async with``."""
        if self._pylon is None:
            raise RuntimeError(
                "ChainClient used outside its async context manager"
            )
        return self._pylon

    # --- Neuron discovery ---

    async def get_recent_neurons(self, netuid: int) -> list[NeuronInfo]:
        """Fetch the current metagraph from Pylon's cached recent-neurons endpoint.

        Args:
            netuid: Subnet netuid to fetch.

        Returns:
            One :class:`NeuronInfo` per registered neuron on the subnet.

        Raises:
            ChainConnectionError: When Pylon is unreachable.
            ChainTimeoutError: When the request exceeds the timeout.
        """
        pylon = self._ensure_pylon()
        try:
            raw = await pylon.v1.open_access.get_recent_neurons(netuid=netuid)
        except TimeoutError as e:
            raise ChainTimeoutError(
                f"get_recent_neurons(netuid={netuid}) timed out"
            ) from e
        except Exception as e:
            raise ChainConnectionError(
                f"get_recent_neurons(netuid={netuid}) failed: {e}"
            ) from e
        return [NeuronInfo.from_pylon(n) for n in raw]

    # --- Block + extrinsic reads ---

    async def get_latest_block(self) -> BlockInfo:
        """Fetch the most recent block from Pylon.

        Raises:
            ChainConnectionError: When Pylon is unreachable.
            ChainTimeoutError: When the request exceeds the timeout.
        """
        pylon = self._ensure_pylon()
        try:
            raw = await pylon.v1.open_access.get_latest_block()
        except TimeoutError as e:
            raise ChainTimeoutError("get_latest_block timed out") from e
        except Exception as e:
            raise ChainConnectionError(f"get_latest_block failed: {e}") from e
        return BlockInfo.from_pylon(raw)

    async def get_extrinsic(
        self, block_number: int, extrinsic_index: int
    ) -> ExtrinsicInfo:
        """Fetch an extrinsic by ``(block_number, extrinsic_index)`` with success status.

        Internally calls :meth:`check_extrinsic_success` so callers receive an
        :class:`ExtrinsicInfo` with ``succeeded`` already populated. If the
        success check itself fails, ``succeeded`` stays ``None`` and a warning
        is logged — the caller can decide whether to retry the check directly.

        Args:
            block_number: Block number containing the extrinsic.
            extrinsic_index: Zero-based index of the extrinsic within the block.

        Returns:
            :class:`ExtrinsicInfo` with ``succeeded`` populated when resolvable.

        Raises:
            ChainConnectionError: When Pylon is unreachable.
            ChainTimeoutError: When the Pylon request exceeds the timeout.
            ExtrinsicNotFoundError: When no extrinsic exists at the given index.
        """
        pylon = self._ensure_pylon()
        try:
            raw = await pylon.v1.open_access.get_extrinsic(
                block_number=block_number,
                extrinsic_index=extrinsic_index,
            )
        except TimeoutError as e:
            raise ChainTimeoutError(
                f"get_extrinsic(block={block_number}, idx={extrinsic_index}) timed out"
            ) from e
        except Exception as e:
            msg = str(e).lower()
            if "not found" in msg or "404" in msg:
                raise ExtrinsicNotFoundError(
                    f"no extrinsic at block={block_number}, idx={extrinsic_index}"
                ) from e
            raise ChainConnectionError(
                f"get_extrinsic(block={block_number}, idx={extrinsic_index}) "
                f"failed: {e}"
            ) from e

        block_hash = str(getattr(raw, "block_hash", "") or "")
        succeeded: bool | None = None
        if block_hash:
            try:
                succeeded = await self.check_extrinsic_success(
                    block_hash, extrinsic_index
                )
            except ChainError:
                logger.warning(
                    "could not resolve success status for extrinsic "
                    f"(block={block_number}, idx={extrinsic_index}); leaving as None",
                    exc_info=True,
                )

        return ExtrinsicInfo.from_pylon(
            raw,
            block_number=block_number,
            extrinsic_index=extrinsic_index,
            succeeded=succeeded,
        )

    # --- Weight setting ---

    async def put_weights(self, weights: dict[str, float]) -> None:
        """Submit a weight vector via Pylon ``identity.put_weights``.

        Pylon handles the underlying retries (~200x across the epoch) and
        commit-reveal vs direct emission detection from subnet hyperparams.

        Args:
            weights: Mapping from hotkey SS58 to weight in [0, 1]. Sum need
                not equal 1; Pylon normalizes.

        Raises:
            ChainConnectionError: When Pylon is unreachable.
            ChainTimeoutError: When the request exceeds the timeout.
        """
        pylon = self._ensure_pylon()
        try:
            await pylon.identity.put_weights(weights=weights)
        except TimeoutError as e:
            raise ChainTimeoutError("put_weights timed out") from e
        except Exception as e:
            raise ChainConnectionError(f"put_weights failed: {e}") from e
        logger.info(
            f"put_weights submitted for netuid={self._config.netuid} "
            f"with {len(weights)} entries"
        )

    # --- Success status (Pylon gap) ---

    async def check_extrinsic_success(
        self, block_hash: str, extrinsic_index: int
    ) -> bool:
        """Read ``system.Events`` at ``block_hash`` to resolve extrinsic success.

        Pylon does NOT surface ``system.ExtrinsicSuccess`` / ``ExtrinsicFailed``
        events; this method fills that gap via ``async-substrate-interface``.

        Args:
            block_hash: Block hash containing the extrinsic.
            extrinsic_index: Zero-based index of the extrinsic within the block.

        Returns:
            ``True`` on ``ExtrinsicSuccess`` at the matching index,
            ``False`` on ``ExtrinsicFailed``.

        Raises:
            ChainConnectionError: When the substrate node is unreachable.
            ChainTimeoutError: When the events query exceeds its timeout.
            ExtrinsicNotFoundError: When neither success nor failure event is
                found for ``extrinsic_index`` at the block.
        """
        from async_substrate_interface import AsyncSubstrateInterface

        try:
            async with AsyncSubstrateInterface(
                url=self._substrate_url()
            ) as substrate:
                events = await substrate.query(
                    module=_SYSTEM_MODULE,
                    storage_function="Events",
                    block_hash=block_hash,
                )
        except TimeoutError as e:
            raise ChainTimeoutError(
                f"check_extrinsic_success({block_hash}, {extrinsic_index}) "
                "timed out"
            ) from e
        except Exception as e:
            raise ChainConnectionError(
                f"check_extrinsic_success({block_hash}, {extrinsic_index}) "
                f"failed: {e}"
            ) from e

        for record in _iter_event_records(events):
            phase = record.get("phase") or {}
            applied = phase.get("ApplyExtrinsic")
            if applied is None or int(applied) != extrinsic_index:
                continue
            event = record.get("event") or {}
            module_id = event.get("module_id") or event.get("module")
            event_id = event.get("event_id") or event.get("name")
            if module_id == _SYSTEM_MODULE and event_id == _EXTRINSIC_SUCCESS_EVENT:
                return True
            if module_id == _SYSTEM_MODULE and event_id == _EXTRINSIC_FAILED_EVENT:
                return False

        raise ExtrinsicNotFoundError(
            f"no ExtrinsicSuccess/Failed event for index {extrinsic_index} "
            f"at block {block_hash}"
        )

    def _substrate_url(self) -> str:
        """Resolve substrate WebSocket URL for the configured network identifier."""
        network = self._config.subtensor_network
        if network == "finney":
            return _FINNEY_WS_URL
        if network == "test":
            return _TEST_WS_URL
        if network == "local":
            return _LOCAL_WS_URL
        return network


def _iter_event_records(events: Any) -> list[dict[str, Any]]:
    """Normalize a substrate query result into a list of event-record dicts.

    The exact shape returned by ``async-substrate-interface`` depends on the
    library version; accept a list, a ``.value``-wrapped object, or anything
    that looks like a sequence of dict-like records.
    """
    if events is None:
        return []
    value = getattr(events, "value", events)
    if isinstance(value, list):
        return [dict(r) for r in value if isinstance(r, dict)]
    return []
