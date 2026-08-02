"""ChainClient: async context manager wrapping Pylon and substrate-interface."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

from ditto.chain.errors import (
    ChainAuthError,
    ChainConnectionError,
    ChainTimeoutError,
    ExtrinsicNotFoundError,
)
from ditto.chain.models import (
    BlockInfo,
    ChainConfig,
    ChainWeight,
    ChainWeightsSnapshot,
    ChainWeightVector,
    ExtrinsicInfo,
    NeuronInfo,
)

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

# --- substrate storage identifiers for payment-verifier reads ---

_SUBTENSOR_MODULE = "SubtensorModule"
_OWNER_STORAGE = "Owner"
_SUBNET_OWNER_HOTKEY_STORAGE = "SubnetOwnerHotkey"
_KEYS_STORAGE = "Keys"
_WEIGHTS_STORAGE = "Weights"
_TIMESTAMP_MODULE = "Timestamp"
_TIMESTAMP_NOW_STORAGE = "Now"


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

    async def get_registered_coldkey(self, hotkey: str, netuid: int) -> str | None:
        """Return the current coldkey owner when ``hotkey`` is registered.

        This uses the same recent-neurons snapshot as :meth:`is_registered`, so
        pre-payment policy checks can bind to ownership without a second chain
        backend or trusting a miner-supplied coldkey.
        """
        neurons = await self.get_recent_neurons(netuid)
        return next((n.coldkey for n in neurons if n.hotkey == hotkey), None)

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

    async def get_block_hash(self, block_number: int) -> str:
        """Resolve the canonical hash for ``block_number`` from Substrate.

        Payment proofs carry both identifiers because Pylon reads extrinsics by
        number while historical events and storage are read by hash. Callers
        must bind the pair before combining those two data sources.
        """
        from async_substrate_interface import AsyncSubstrateInterface

        try:
            async with AsyncSubstrateInterface(url=self._substrate_url()) as substrate:
                block_hash = await substrate.get_block_hash(block_number)
        except TimeoutError as e:
            raise ChainTimeoutError(f"get_block_hash({block_number}) timed out") from e
        except Exception as e:
            raise ChainConnectionError(
                f"get_block_hash({block_number}) failed: {self._safe_rpc_error(e)}"
            ) from e
        if not block_hash:
            raise ExtrinsicNotFoundError(
                f"no block hash found for block number {block_number}"
            )
        return str(block_hash).lower()

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

    async def get_weights(self, netuid: int) -> ChainWeightsSnapshot:
        """Read the latest publicly revealed validator weight matrix natively.

        ``SubtensorModule.Weights`` is the chain's public, last-revealed matrix.
        Under commit-reveal it intentionally lags encrypted active commitments;
        callers must present it as observed chain state, not as a pending vector
        or a direct miner-emission calculation.
        """
        from async_substrate_interface import AsyncSubstrateInterface

        try:
            async with AsyncSubstrateInterface(url=self._substrate_url()) as substrate:
                block_hash = await substrate.get_chain_head()
                header = await substrate.get_block_header(block_hash=block_hash)
                block = _block_number_from_header(header)
                weight_map = await substrate.query_map(
                    module=_SUBTENSOR_MODULE,
                    storage_function=_WEIGHTS_STORAGE,
                    params=[netuid],
                    block_hash=block_hash,
                    fully_exhaust=True,
                )
                key_map = await substrate.query_map(
                    module=_SUBTENSOR_MODULE,
                    storage_function=_KEYS_STORAGE,
                    params=[netuid],
                    block_hash=block_hash,
                    fully_exhaust=True,
                )
                owner_result = await substrate.query(
                    module=_SUBTENSOR_MODULE,
                    storage_function=_SUBNET_OWNER_HOTKEY_STORAGE,
                    params=[netuid],
                    block_hash=block_hash,
                )
                raw_weights = [
                    (int(uid), value or []) async for uid, value in weight_map
                ]
                hotkeys = {
                    int(uid): str(hotkey) async for uid, hotkey in key_map if hotkey
                }
                owner_hotkey_value = _unwrap_substrate_value(owner_result)
        except TimeoutError as e:
            raise ChainTimeoutError(f"get_weights(netuid={netuid}) timed out") from e
        except Exception as e:
            raise ChainConnectionError(
                f"get_weights(netuid={netuid}) failed: {e}"
            ) from e

        vectors = []
        for validator_uid, raw_vector in raw_weights:
            validator_hotkey = hotkeys.get(validator_uid)
            if not validator_hotkey or not isinstance(raw_vector, (list, tuple)):
                continue
            weights = []
            for item in raw_vector:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    continue
                uid, value = int(item[0]), int(item[1])
                hotkey = hotkeys.get(uid)
                if hotkey and value > 0:
                    weights.append(ChainWeight(uid=uid, hotkey=hotkey, value=value))
            if weights:
                vectors.append(
                    ChainWeightVector(
                        validator_uid=validator_uid,
                        validator_hotkey=validator_hotkey,
                        weights=tuple(weights),
                    )
                )
        return ChainWeightsSnapshot(
            netuid=netuid,
            block=block,
            block_hash=str(block_hash),
            owner_hotkey=(str(owner_hotkey_value) if owner_hotkey_value else None),
            vectors=tuple(vectors),
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
            events = await self._query_historical_storage(
                AsyncSubstrateInterface,
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
                f"check_extrinsic_success({block_hash}, {extrinsic_index}) failed: "
                f"{self._safe_rpc_error(e)}"
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

    async def get_coldkey_for_hotkey(self, hotkey: str, block_hash: str) -> str:
        """Read ``SubtensorModule.Owner(hotkey)`` at ``block_hash``.

        Used by :mod:`ditto.api_server.payment_verifier` to confirm the
        payment extrinsic was signed by the same coldkey that owns the
        claimed hotkey at payment time. Pylon does not expose historical
        coldkey ownership keyed by ``block_hash``, so this method fills
        the gap via ``async-substrate-interface`` in the same shape as
        :meth:`check_extrinsic_success`.

        Args:
            hotkey: SS58-encoded hotkey to look up the owner for.
            block_hash: Block hash at which to read the storage entry.

        Returns:
            SS58-encoded coldkey that owns ``hotkey`` at ``block_hash``.

        Raises:
            ExtrinsicNotFoundError: When the storage entry is empty
                (hotkey was not registered at that block).
            ChainConnectionError: When the substrate node is unreachable.
            ChainTimeoutError: When the query exceeds its timeout.
        """
        from async_substrate_interface import AsyncSubstrateInterface

        try:
            result = await self._query_historical_storage(
                AsyncSubstrateInterface,
                module=_SUBTENSOR_MODULE,
                storage_function=_OWNER_STORAGE,
                params=[hotkey],
                block_hash=block_hash,
            )
        except TimeoutError as e:
            raise ChainTimeoutError(
                f"get_coldkey_for_hotkey({hotkey}, {block_hash}) timed out"
            ) from e
        except Exception as e:
            raise ChainConnectionError(
                f"get_coldkey_for_hotkey({hotkey}, {block_hash}) failed: "
                f"{self._safe_rpc_error(e)}"
            ) from e

        coldkey = _unwrap_substrate_value(result)
        if not coldkey:
            raise ExtrinsicNotFoundError(
                f"no Owner entry for hotkey {hotkey} at block {block_hash}"
            )
        return str(coldkey)

    async def get_block_timestamp(self, block_hash: str) -> int:
        """Read ``Timestamp.Now`` at ``block_hash`` and return seconds.

        Substrate's ``pallet_timestamp`` stores the block time as a u64
        millisecond unix timestamp. The payment verifier needs seconds
        to populate the ``evaluation_payments.timestamp`` column (which
        is a tz-aware ``datetime`` built from this value). Conversion
        happens here so callers never see the ms representation.

        Args:
            block_hash: Block hash at which to read the storage entry.

        Returns:
            Block timestamp as unix seconds (integer).

        Raises:
            ExtrinsicNotFoundError: When the storage entry is empty
                (the block does not exist in the archive).
            ChainConnectionError: When the substrate node is unreachable.
            ChainTimeoutError: When the query exceeds its timeout.
        """
        from async_substrate_interface import AsyncSubstrateInterface

        try:
            result = await self._query_historical_storage(
                AsyncSubstrateInterface,
                module=_TIMESTAMP_MODULE,
                storage_function=_TIMESTAMP_NOW_STORAGE,
                block_hash=block_hash,
            )
        except TimeoutError as e:
            raise ChainTimeoutError(
                f"get_block_timestamp({block_hash}) timed out"
            ) from e
        except Exception as e:
            raise ChainConnectionError(
                f"get_block_timestamp({block_hash}) failed: {self._safe_rpc_error(e)}"
            ) from e

        raw = _unwrap_substrate_value(result)
        if raw is None:
            raise ExtrinsicNotFoundError(
                f"no Timestamp.Now entry at block {block_hash}"
            )
        # Substrate ``pallet_timestamp`` stores milliseconds. Convert to
        # seconds at the boundary so downstream code never sees ms.
        return int(raw) // 1000

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

    def _configured_archive_url(self) -> str | None:
        """Return the configured provider URL with its credential attached."""
        url = self._config.archive_rpc_url
        if not url:
            return None
        api_key = self._config.archive_rpc_api_key
        auth_mode = self._config.archive_rpc_auth_mode
        if not api_key or auth_mode == "none":
            return url
        if auth_mode == "path":
            return f"{url.rstrip('/')}/{quote(api_key, safe='')}"
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}authorization={quote(api_key, safe='')}"

    def _historical_substrate_urls(self) -> tuple[str, ...]:
        """Return the free-first archive try list, then configured paid RPC."""
        urls = list(self._config.public_archive_rpc_urls)
        configured_url = self._configured_archive_url()
        if configured_url:
            urls.append(configured_url)
        if not urls:
            urls.append(self._substrate_url())
        # Preserve order while preventing duplicate calls when an operator
        # configures one of the built-in public endpoints explicitly.
        return tuple(dict.fromkeys(urls))

    async def _query_historical_storage(
        self,
        substrate_factory: Any,
        *,
        module: str,
        storage_function: str,
        block_hash: str,
        params: list[Any] | None = None,
    ) -> Any:
        """Query archive providers in order and return the first successful read."""
        failures: list[tuple[str, Exception]] = []
        for url in self._historical_substrate_urls():
            provider = urlsplit(url).hostname or "configured archive"
            try:
                async with asyncio.timeout(self._config.archive_rpc_timeout_seconds):
                    async with substrate_factory(url=url) as substrate:
                        query_kwargs: dict[str, Any] = {
                            "module": module,
                            "storage_function": storage_function,
                            "block_hash": block_hash,
                        }
                        if params is not None:
                            query_kwargs["params"] = params
                        return await substrate.query(**query_kwargs)
            except Exception as error:
                failures.append((provider, error))
                logger.warning(
                    "historical archive read failed provider=%s error=%s",
                    provider,
                    self._safe_rpc_error(error),
                )

        summary = "; ".join(
            f"{provider}: {self._safe_rpc_error(error)}" for provider, error in failures
        )
        if failures and all(isinstance(error, TimeoutError) for _, error in failures):
            raise TimeoutError(summary)
        raise RuntimeError(summary or "no historical archive endpoint configured")

    def _safe_rpc_error(self, error: Exception) -> str:
        """Render an RPC exception with configured credentials redacted."""
        detail = str(error)
        api_key = self._config.archive_rpc_api_key
        if not api_key:
            return detail
        return detail.replace(api_key, "<redacted>").replace(
            quote(api_key, safe=""), "<redacted>"
        )


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


def _block_number_from_header(header: Any) -> int:
    """Extract the decoded block number from async-substrate header shapes."""
    if isinstance(header, dict):
        nested = header.get("header")
        source = nested if isinstance(nested, dict) else header
        value = source.get("number")
        if isinstance(value, str):
            return int(value, 0)
        if isinstance(value, int):
            return value
    raise ValueError("substrate block header omitted a valid number")


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


def _unwrap_substrate_value(result: Any) -> Any:
    """Return the inner ``value`` for a scalar substrate storage query.

    ``async-substrate-interface`` wraps scalar storage reads in an object
    exposing ``.value``; some versions return the value directly. Treat
    empty / ``None`` results uniformly so callers can branch on a single
    falsy check.
    """
    if result is None:
        return None
    return getattr(result, "value", result)
