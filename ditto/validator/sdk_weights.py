"""On-chain weight submission via the bittensor SDK (deprecated fallback).

Pylon is the supported weight path (see VALIDATOR.md); its identity
``put_weights`` requires a registered write identity. This escape hatch exists
only for bring-up on a chain where Pylon is not yet stood up. When
``VALIDATOR_USE_SDK_WEIGHTS`` is set the worker submits weights directly through
the bittensor SDK
(``Subtensor.set_weights``) instead. :class:`SdkWeightSetter` duck-types
:meth:`ditto.chain.ChainClient.put_weights` (``async def put_weights(weights)``)
so :class:`~ditto.validator.worker.ValidatorWorker` is agnostic to the sink.

The validator's hotkey keypair (already loaded in-memory via
``load_validator_keypair`` -- wallet file or ``VALIDATOR_MNEMONIC``) is wrapped
in an ephemeral ``bittensor.wallet`` with no on-disk files; ``set_weights`` is
hotkey-signed, so the coldkey is never needed. The blocking SDK call runs in a
worker thread so the async sweep loop is not stalled.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from typing import TYPE_CHECKING, Any

from ditto.validator.errors import WeightSubmissionError

if TYPE_CHECKING:
    from ditto.validator.config import ValidatorConfig

logger = logging.getLogger(__name__)


class SdkWeightSetter:
    """Submit weights via ``bittensor.Subtensor.set_weights`` (localnet path)."""

    def __init__(self, config: ValidatorConfig, keypair: Any) -> None:
        self._config = config
        self._keypair = keypair
        self._subtensor: Any = None
        self._wallet: Any = None

    def _ensure(self) -> None:
        """Lazily build the subtensor connection + an in-memory wallet."""
        import bittensor

        if self._subtensor is None:
            # Same pattern as the miner CLI: a ws:// endpoint is passed straight
            # through as ``network`` (SDK treats it as the chain endpoint).
            self._subtensor = bittensor.Subtensor(
                network=self._config.subtensor_network
            )
        if self._wallet is None:
            # bittensor's Wallet is a Rust/pyo3 object (no attribute injection),
            # so load the keypair via set_hotkey into an isolated temp wallet dir
            # rather than ~/.bittensor. ``set_weights`` is hotkey-signed, so the
            # coldkey is never written. The dir is process-local and ephemeral.
            tmp = tempfile.mkdtemp(prefix="ditto-validator-sdk-")
            wallet = bittensor.Wallet(name="validator-sdk", path=tmp)
            wallet.set_hotkey(self._keypair, encrypt=False, overwrite=True)
            self._wallet = wallet

    async def put_weights(self, weights: dict[str, float]) -> None:
        """Resolve hotkeys -> UIDs and submit a weight vector on chain."""
        if not weights:
            return
        await asyncio.to_thread(self._put_weights_sync, weights)

    async def has_validator_permit(self, hotkey: str, netuid: int) -> bool | None:
        """Whether ``hotkey`` holds a validator permit on ``netuid``.

        ``None`` when the hotkey isn't registered (so the caller can't decide).
        Runs the blocking SDK reads in a thread so the sweep loop isn't stalled.
        """
        return await asyncio.to_thread(self._has_validator_permit_sync, hotkey, netuid)

    def _has_validator_permit_sync(self, hotkey: str, netuid: int) -> bool | None:
        self._ensure()
        uid = self._subtensor.get_uid_for_hotkey_on_subnet(hotkey, netuid)
        if uid is None:
            return None
        neuron = self._subtensor.neuron_for_uid(int(uid), netuid)
        return bool(getattr(neuron, "validator_permit", False))

    async def get_stake_tao(self, hotkey: str, netuid: int) -> float | None:
        """Stake (TAO) on ``hotkey``'s neuron, or ``None`` when not registered.

        The min-stake companion to :meth:`has_validator_permit`. Runs the
        blocking SDK reads in a thread so the sweep loop isn't stalled.
        """
        return await asyncio.to_thread(self._get_stake_tao_sync, hotkey, netuid)

    def _get_stake_tao_sync(self, hotkey: str, netuid: int) -> float | None:
        self._ensure()
        uid = self._subtensor.get_uid_for_hotkey_on_subnet(hotkey, netuid)
        if uid is None:
            return None
        neuron = self._subtensor.neuron_for_uid(int(uid), netuid)
        stake = getattr(neuron, "stake", None)
        if stake is None:
            return None
        # bittensor's Balance exposes ``.tao``; a plain number passes through.
        return float(getattr(stake, "tao", stake))

    async def get_tempo(self, netuid: int) -> int | None:
        """The subnet's ``Tempo`` hyperparameter (blocks per epoch)."""
        return await asyncio.to_thread(self._get_tempo_sync, netuid)

    def _get_tempo_sync(self, netuid: int) -> int | None:
        self._ensure()
        tempo = self._subtensor.tempo(netuid)
        return None if tempo is None else int(tempo)

    async def get_weights_rate_limit(self, netuid: int) -> int | None:
        """The subnet's ``WeightsSetRateLimit`` hyperparameter (blocks)."""
        return await asyncio.to_thread(self._get_weights_rate_limit_sync, netuid)

    def _get_weights_rate_limit_sync(self, netuid: int) -> int | None:
        self._ensure()
        limit = self._subtensor.weights_rate_limit(netuid)
        return None if limit is None else int(limit)

    async def get_commit_reveal_enabled(self, netuid: int) -> bool | None:
        """Whether the subnet runs commit-reveal (``CommitRevealWeightsEnabled``).

        Observability only: ``set_weights`` already routes to the timelock
        commit itself when this is on (commit-reveal v3), so the worker just
        logs the mode. ``None`` when the read is undeterminable.
        """
        return await asyncio.to_thread(self._get_commit_reveal_enabled_sync, netuid)

    def _get_commit_reveal_enabled_sync(self, netuid: int) -> bool | None:
        self._ensure()
        try:
            # bittensor's commit_reveal_enabled asserts a non-None hyperparameter;
            # treat any read failure as undeterminable rather than raising.
            return bool(self._subtensor.commit_reveal_enabled(netuid))
        except Exception:  # noqa: BLE001 - observability read must not raise
            return None

    async def get_reveal_period_epochs(self, netuid: int) -> int | None:
        """The subnet's ``RevealPeriodEpochs`` (epochs from commit to reveal)."""
        return await asyncio.to_thread(self._get_reveal_period_epochs_sync, netuid)

    def _get_reveal_period_epochs_sync(self, netuid: int) -> int | None:
        self._ensure()
        try:
            period = self._subtensor.get_subnet_reveal_period_epochs(netuid)
        except Exception:  # noqa: BLE001 - advisory read must not raise
            return None
        return None if period is None else int(period)

    def _put_weights_sync(self, weights: dict[str, float]) -> None:
        self._ensure()
        netuid = self._config.netuid
        uids: list[int] = []
        values: list[float] = []
        for hotkey, weight in weights.items():
            uid = self._subtensor.get_uid_for_hotkey_on_subnet(hotkey, netuid)
            if uid is None:
                logger.warning(
                    "hotkey %s not registered on netuid %d; skipping in weight vector",
                    hotkey,
                    netuid,
                )
                continue
            uids.append(int(uid))
            values.append(float(weight))

        if not uids:
            logger.warning("no resolvable miner UIDs; skipping set_weights")
            return

        logger.info(
            "SDK set_weights netuid=%d as %s -> uids=%s weights=%s",
            netuid,
            self._config.validator_hotkey,
            uids,
            values,
        )
        try:
            response = self._subtensor.set_weights(
                wallet=self._wallet,
                netuid=netuid,
                uids=uids,
                weights=values,
                # Stamp the mechanism version so the chain doesn't average our
                # weights against a validator scoring under a different version.
                version_key=self._config.weight_version_key,
                wait_for_inclusion=True,
                # Don't block the sweep on finalization / commit-reveal execution;
                # inclusion of the (commit or direct) extrinsic is enough here.
                wait_for_finalization=False,
                wait_for_revealed_execution=False,
                raise_error=False,
            )
        except Exception as e:  # noqa: BLE001 - surface any SDK failure uniformly
            raise WeightSubmissionError(f"set_weights raised: {e}") from e

        # set_weights returns either a (success: bool, message: str) tuple or an
        # ExtrinsicResponse-like object depending on the bittensor version.
        if isinstance(response, tuple):
            success, message = response[0], (response[1] if len(response) > 1 else "")
        else:
            success = getattr(response, "success", None)
            message = getattr(response, "error_message", None) or getattr(
                response, "message", None
            )
        if success is False:
            raise WeightSubmissionError(f"set_weights failed: {message or response!r}")
        logger.info("SDK set_weights accepted (msg=%s)", message)
