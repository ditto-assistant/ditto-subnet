"""ChainClient: async context manager wrapping Pylon and substrate-interface."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ditto.chain.errors import (
    ChainAuthError,
    ChainConnectionError,
    ChainTimeoutError,
    ExtrinsicNotFoundError,
)
from ditto.chain.models import BlockInfo, ChainConfig, ExtrinsicInfo, NeuronInfo

if TYPE_CHECKING:
    from types import TracebackType

    from pylon_client.artanis import AsyncPylonClient

logger = logging.getLogger(__name__)


# --- Substrate WebSocket URLs (used only for the Pylon events gap) ---

_FINNEY_WS_URL = "wss://entrypoint-finney.opentensor.ai:443"
_TEST_WS_URL = "wss://test.finney.opentensor.ai:443"
_LOCAL_WS_URL = "ws://127.0.0.1:9944"

# --- substrate ``System.Events`` identifiers we filter on ---

_APPLY_EXTRINSIC_PHASE = "ApplyExtrinsic"
_SYSTEM_MODULE = "System"
_EXTRINSIC_SUCCESS_EVENT = "ExtrinsicSuccess"
_EXTRINSIC_FAILED_EVENT = "ExtrinsicFailed"


class ChainClient:
    """Async context manager wrapping Pylon for chain access.

    Holds an :class:`AsyncPylonClient` for the duration of the ``async with``
    block. Two consumer processes in the Ditto codebase:

    - **API server** (open-access mode): reads neurons, blocks, extrinsics,
      events. Does not write. Used by ``ditto.api.payment_verifier``,
      ``ditto.api.loops``, and the request handlers under
      ``ditto.api.endpoints``.
    - **Validator daemon** (identity mode): same read surface plus
      :meth:`put_weights` for weight emission. Identity is mandatory because
      ``put_weights`` is an identity-only endpoint.

    ``ditto.miner_cli`` is NOT a consumer - it uses raw bittensor SDK
    directly per the locked architecture exception.

    Extrinsic success / failure detection is the one Pylon gap: Pylon's
    ``Extrinsic`` response carries the call data but no ``ExtrinsicSuccess``
    /``ExtrinsicFailed`` event status, and the block hash needed to read
    events is not in the response either. :meth:`check_extrinsic_success`
    fills the gap via a small ``async-substrate-interface`` read, but the
    caller must supply the block hash (typically obtained from extrinsic
    finalisation on the submitter side).

    Usage:
        async with ChainClient(config) as client:
            block = await client.get_latest_block()
            ext = await client.get_extrinsic(block.number, 0)
            ok = await client.check_extrinsic_success(block.hash, 0)
    """

    def __init__(self, config: ChainConfig) -> None:
        """Store the config; the underlying Pylon client is built in ``__aenter__``."""
        self._config = config
        self._pylon: AsyncPylonClient | None = None

    async def __aenter__(self) -> ChainClient:
        """Open the underlying Pylon client connection."""
        from pylon_client.artanis import AsyncConfig, AsyncPylonClient

        kwargs: dict[str, str] = {"address": self._config.pylon_url}
        if self._config.open_access_token:
            kwargs["open_access_token"] = self._config.open_access_token
        if self._config.identity_name and self._config.identity_token:
            kwargs["identity_name"] = self._config.identity_name
            kwargs["identity_token"] = self._config.identity_token

        try:
            self._pylon = AsyncPylonClient(AsyncConfig(**kwargs))
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
            raise RuntimeError("ChainClient used outside its async context manager")
        return self._pylon

    # --- Neuron discovery ---

    async def get_recent_neurons(self, netuid: int) -> list[NeuronInfo]:
        """Fetch the current metagraph from Pylon's cached recent-neurons endpoint.

        Args:
            netuid: Subnet netuid to fetch.

        Returns:
            One :class:`NeuronInfo` per registered neuron on the subnet.

        Raises:
            ChainConnectionError: When Pylon is unreachable or returns an error.
            ChainTimeoutError: When the request exceeds the configured timeout.
        """
        pylon = self._ensure_pylon()
        try:
            response = await pylon.v1.open_access.get_recent_neurons(netuid)
        except Exception as e:
            raise _translate_pylon_error(
                e, f"get_recent_neurons(netuid={netuid})"
            ) from e
        return [
            NeuronInfo.from_pylon(neuron, hotkey=hotkey)
            for hotkey, neuron in response.neurons.items()
        ]

    async def is_registered(self, hotkey: str, netuid: int) -> bool:
        """Return ``True`` iff ``hotkey`` is registered on ``netuid``.

        Walks the latest :meth:`get_recent_neurons` response. The Pylon
        cache is one block stale at worst (~12 s), acceptable for
        registration checks at HTTP-request scope.

        Raises:
            ChainConnectionError: When Pylon is unreachable.
            ChainTimeoutError: When the request exceeds the configured timeout.
        """
        neurons = await self.get_recent_neurons(netuid)
        return any(n.hotkey == hotkey for n in neurons)

    # --- Block + extrinsic reads ---

    async def get_latest_block(self) -> BlockInfo:
        """Fetch the most recent block info from Pylon.

        Wraps Pylon's ``get_latest_block_info`` which returns a
        ``BlockInfoBag`` (number + hash + timestamp).

        Raises:
            ChainConnectionError: When Pylon is unreachable or returns an error.
            ChainTimeoutError: When the request exceeds the configured timeout.
        """
        pylon = self._ensure_pylon()
        try:
            response = await pylon.v1.open_access.get_latest_block_info()
        except Exception as e:
            raise _translate_pylon_error(e, "get_latest_block_info") from e
        return BlockInfo.from_pylon(response)

    async def get_extrinsic(
        self, block_number: int, extrinsic_index: int
    ) -> ExtrinsicInfo:
        """Fetch an extrinsic by ``(block_number, extrinsic_index)``.

        The returned :class:`ExtrinsicInfo` has ``succeeded=None``. Pylon's
        response does not include the block hash, so success-event lookup
        cannot be performed from this call alone. Callers that hold the
        block hash should call :meth:`check_extrinsic_success` separately
        and replace ``succeeded`` on a new :class:`ExtrinsicInfo` if needed.

        Args:
            block_number: Block number containing the extrinsic.
            extrinsic_index: Zero-based index of the extrinsic within the block.

        Returns:
            :class:`ExtrinsicInfo` populated from Pylon's response with
            ``succeeded=None``.

        Raises:
            ExtrinsicNotFoundError: When no extrinsic exists at the index.
            ChainConnectionError: When Pylon is unreachable or returns an error.
            ChainTimeoutError: When the request exceeds the configured timeout.
        """
        pylon = self._ensure_pylon()
        try:
            response = await pylon.v1.open_access.get_extrinsic(
                block_number, extrinsic_index
            )
        except Exception as e:
            raise _translate_pylon_error(
                e,
                f"get_extrinsic(block={block_number}, idx={extrinsic_index})",
            ) from e
        return ExtrinsicInfo.from_pylon(response)

    # --- Weight setting ---

    async def put_weights(self, weights: dict[str, float]) -> None:
        """Submit a weight vector via Pylon ``identity.put_weights``.

        Pylon handles the underlying retries (~200x across the epoch) and
        commit-reveal vs direct emission detection from subnet hyperparams.

        Args:
            weights: Mapping from hotkey SS58 to weight in [0, 1]. Sum need
                not equal 1; Pylon normalises. Hotkey and Weight are
                ``NewType`` aliases over ``str`` and ``float`` in Pylon, so
                plain values are accepted at runtime.

        Raises:
            ChainAuthError: When the client was opened without an identity or
                when the configured identity lacks the validator permit / stake
                Pylon requires to accept a weight submission.
            ChainConnectionError: When Pylon is unreachable or returns an
                unexpected non-auth error.
            ChainTimeoutError: When the request exceeds the configured timeout.
        """
        pylon = self._ensure_pylon()
        try:
            await pylon.v1.identity.put_weights(weights)
        except Exception as e:
            raise _translate_pylon_error(e, "put_weights") from e
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
        The main caller is ``ditto.api.payment_verifier``, which uses this to
        confirm a miner's upload-payment extrinsic actually executed
        successfully on chain after Pylon has confirmed its call args.

        Args:
            block_hash: Block hash containing the extrinsic. Typically
                obtained from extrinsic finalisation on the submitter side.
            extrinsic_index: Zero-based index of the extrinsic within the block.

        Returns:
            ``True`` on ``ExtrinsicSuccess`` at the matching index,
            ``False`` on ``ExtrinsicFailed``.

        Raises:
            ExtrinsicNotFoundError: When neither success nor failure event is
                found for ``extrinsic_index`` at the block.
            ChainConnectionError: When the substrate node is unreachable.
            ChainTimeoutError: When the events query exceeds its timeout.
        """
        from async_substrate_interface import AsyncSubstrateInterface

        try:
            async with AsyncSubstrateInterface(url=self._substrate_url()) as substrate:
                events = await substrate.query(
                    module=_SYSTEM_MODULE,
                    storage_function="Events",
                    block_hash=block_hash,
                )
        except TimeoutError as e:
            raise ChainTimeoutError(
                f"check_extrinsic_success({block_hash}, {extrinsic_index}) timed out"
            ) from e
        except Exception as e:
            raise ChainConnectionError(
                f"check_extrinsic_success({block_hash}, {extrinsic_index}) failed: {e}"
            ) from e

        for record in _iter_event_records(events):
            # Each record from async-substrate-interface is a flat dict with
            # ``phase`` (str), ``extrinsic_idx`` (int | None), ``module_id``,
            # ``event_id``, plus nested ``event`` data we don't need here.
            if record.get("phase") != _APPLY_EXTRINSIC_PHASE:
                continue
            if record.get("extrinsic_idx") != extrinsic_index:
                continue
            if record.get("module_id") != _SYSTEM_MODULE:
                continue
            event_id = record.get("event_id")
            if event_id == _EXTRINSIC_SUCCESS_EVENT:
                return True
            if event_id == _EXTRINSIC_FAILED_EVENT:
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


def _translate_pylon_error(exc: Exception, op: str) -> Exception:
    """Map a Pylon SDK exception to a :class:`ChainError` subclass.

    Imports Pylon's exception module lazily so unit tests that stub
    ``pylon_client`` via ``sys.modules`` do not need the real types.
    Falls back to :class:`ChainConnectionError` when the SDK is not
    importable or the exception is not a Pylon type.

    Mapping:

    - ``PylonNotFound`` -> :class:`ExtrinsicNotFoundError`
    - ``PylonTimeoutException`` or stdlib ``TimeoutError`` -> :class:`ChainTimeoutError`
    - ``PylonUnauthorized`` or ``PylonForbidden`` -> :class:`ChainAuthError`
    - ``PylonClosed`` or anything else -> :class:`ChainConnectionError`
    """
    try:
        from pylon_client.artanis import (
            PylonClosed,
            PylonForbidden,
            PylonNotFound,
            PylonTimeoutException,
            PylonUnauthorized,
        )
    except Exception:
        return ChainConnectionError(f"{op} failed: {exc}")

    if isinstance(exc, PylonNotFound):
        return ExtrinsicNotFoundError(f"{op} not found: {exc}")
    if isinstance(exc, PylonTimeoutException):
        return ChainTimeoutError(f"{op} timed out: {exc}")
    if isinstance(exc, (PylonUnauthorized, PylonForbidden)):
        return ChainAuthError(f"{op} rejected by Pylon auth: {exc}")
    if isinstance(exc, PylonClosed):
        return ChainConnectionError(f"{op} on closed client: {exc}")
    if isinstance(exc, TimeoutError):
        return ChainTimeoutError(f"{op} timed out: {exc}")
    return ChainConnectionError(f"{op} failed: {exc}")


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
