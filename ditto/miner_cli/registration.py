"""Neuron registration on the subnet, via the raw bittensor SDK.

``POST /upload/check`` rejects an unregistered hotkey with code 1101
before any TAO moves. That rejection is a dead end the miner has to
leave the CLI to resolve, even though the CLI is already holding the
loaded wallet and a chain connection at the moment it happens. This
module supplies the two chain operations that close the loop in place:
quote the live recycle cost, and submit the
``SubtensorModule.burned_register`` extrinsic signed by the coldkey.

Same architecture lock as :mod:`ditto.miner_cli.payment`: the raw
bittensor SDK rather than Pylon, and the same fail-closed posture --
any pre-submission read that cannot be resolved aborts before the burn
instead of guessing. Registration recycles real TAO and cannot be
reversed or refunded, so the cost is never assumed, cached, or carried
over from a previous run: it is read from chain immediately before the
confirmation prompt and read again immediately before submission.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ditto.miner_cli.errors import (
    RegistrationNotNeededError,
    RegistrationSubmissionError,
)
from ditto.miner_cli.models import RegistrationQuote

if TYPE_CHECKING:
    import bittensor

logger = logging.getLogger(__name__)


def quote_registration(
    *,
    live_wallet: bittensor.Wallet,
    hotkey_ss58: str,
    coldkey_name: str,
    netuid: int,
    subtensor_network: str,
    chain_endpoint: str | None = None,
) -> RegistrationQuote:
    """Read the live cost of registering ``hotkey_ss58`` on ``netuid``.

    Reads, in order: that the subnet exists, that the hotkey is not already
    registered on it, the current recycle amount, and the coldkey's free
    balance. Every one is a chain read taken at call time.

    The already-registered check is what keeps a netuid mismatch from
    costing the miner TAO. A 1101 rejection is issued by the API server
    against the netuid *it* is bound to; this function registers against
    the netuid the *CLI* resolved. When those disagree, the hotkey looks
    registered here and the correct move is to stop.

    Args:
        live_wallet: Live bittensor wallet; ``.coldkeypub`` supplies the
            address whose free balance is quoted.
        hotkey_ss58: Hotkey to register.
        coldkey_name: Wallet coldkey name, carried onto the quote for
            display only.
        netuid: Subnet to register into.
        subtensor_network: Network identifier passed to
            :class:`bittensor.Subtensor`.
        chain_endpoint: Optional explicit chain URL override, used in place
            of ``subtensor_network`` exactly as in
            :func:`ditto.miner_cli.payment.submit_eval_payment`.

    Returns:
        :class:`RegistrationQuote` holding the live recycle amount and
        balance. An unaffordable quote is returned rather than raised, so
        the caller can show the miner the shortfall.

    Raises:
        RegistrationNotNeededError: The hotkey is already registered on
            ``netuid`` according to the chain.
        RegistrationSubmissionError: The subnet is missing, or any read
            needed to price the registration failed.
    """
    chain_target = chain_endpoint or subtensor_network

    try:
        coldkey_ss58 = str(live_wallet.coldkeypub.ss58_address)
    except Exception as e:
        raise RegistrationSubmissionError(
            f"could not read the selected wallet's coldkey address: {e}; "
            "refusing to register because the paying account is unknown"
        ) from e

    subtensor = _connect_subtensor(chain_target)

    try:
        exists = subtensor.subnet_exists(netuid)
    except Exception as e:
        raise RegistrationSubmissionError(
            f"could not check whether netuid {netuid} exists on subtensor "
            f"{chain_target!r}: {e}"
        ) from e
    if not exists:
        raise RegistrationSubmissionError(
            f"netuid {netuid} does not exist on subtensor {chain_target!r}; "
            "no registration was attempted"
        )

    try:
        already_registered = subtensor.is_hotkey_registered(
            hotkey_ss58=hotkey_ss58, netuid=netuid
        )
    except Exception as e:
        raise RegistrationSubmissionError(
            f"could not check registration status for hotkey {hotkey_ss58} "
            f"on netuid {netuid}: {e}; refusing to register"
        ) from e
    if already_registered:
        raise RegistrationNotNeededError(
            f"hotkey {hotkey_ss58} is already registered on netuid {netuid} "
            f"according to subtensor {chain_target!r}, but the platform "
            f"rejected it as unregistered. Confirm the CLI and the platform "
            f"agree on the netuid (env NETUID) and on the network "
            f"(--network), then retry. No TAO was recycled."
        )

    try:
        recycle = subtensor.recycle(netuid=netuid)
    except Exception as e:
        raise RegistrationSubmissionError(
            f"could not read the recycle cost for netuid {netuid}: {e}; "
            "refusing to register at an unknown price"
        ) from e
    if recycle is None:
        raise RegistrationSubmissionError(
            f"subtensor returned no recycle cost for netuid {netuid}; "
            "refusing to register at an unknown price"
        )

    try:
        balance = subtensor.get_balance(coldkey_ss58)
    except Exception as e:
        raise RegistrationSubmissionError(
            f"could not read the free balance of coldkey {coldkey_ss58}: {e}"
        ) from e

    quote = RegistrationQuote(
        netuid=netuid,
        hotkey_ss58=hotkey_ss58,
        coldkey_name=coldkey_name,
        coldkey_ss58=coldkey_ss58,
        recycle_rao=int(recycle.rao),
        balance_rao=int(balance.rao),
    )
    logger.info(
        f"registration quote: netuid={netuid} recycle={quote.recycle_rao} rao "
        f"balance={quote.balance_rao} rao"
    )
    return quote


def submit_registration(
    *,
    live_wallet: bittensor.Wallet,
    hotkey_ss58: str,
    netuid: int,
    subtensor_network: str,
    confirmed_recycle_rao: int,
    chain_endpoint: str | None = None,
) -> int | None:
    """Submit ``burned_register`` and return the assigned uid.

    The recycle amount is re-read immediately before submission and the
    extrinsic is abandoned if it now exceeds ``confirmed_recycle_rao``.
    Registration demand moves the price up by ``burn_increase`` on every
    registration, so between the prompt and a keystroke the cost can be
    higher than what the miner agreed to -- and the chain would recycle
    the new amount without asking. A quote the miner never saw is not
    consent, so a rise aborts and re-quotes on the next run.

    Args:
        live_wallet: Live bittensor wallet; the coldkey signs and the
            recycle amount comes out of its free balance.
        hotkey_ss58: Hotkey being registered, used for the post-submission
            uid lookup and for log lines.
        netuid: Subnet to register into.
        subtensor_network: Network identifier passed to
            :class:`bittensor.Subtensor`.
        confirmed_recycle_rao: The recycle amount the miner confirmed. A
            current cost above this aborts before submission.
        chain_endpoint: Optional explicit chain URL override.

    Returns:
        The uid assigned on ``netuid``, or ``None`` when registration
        succeeded but the uid could not be read back. A ``None`` return is
        cosmetic only: the registration itself is confirmed by then.

    Raises:
        RegistrationSubmissionError: The recycle cost rose above the
            confirmed amount, or the extrinsic was rejected or reported
            failure. The hotkey is unregistered in every case.
    """
    chain_target = chain_endpoint or subtensor_network
    subtensor = _connect_subtensor(chain_target)

    try:
        current = subtensor.recycle(netuid=netuid)
    except Exception as e:
        raise RegistrationSubmissionError(
            f"could not re-read the recycle cost for netuid {netuid} before "
            f"submitting: {e}; no registration was attempted"
        ) from e
    if current is None:
        raise RegistrationSubmissionError(
            f"subtensor returned no recycle cost for netuid {netuid} on the "
            "pre-submission re-read; no registration was attempted"
        )
    current_rao = int(current.rao)
    if current_rao > confirmed_recycle_rao:
        raise RegistrationSubmissionError(
            f"the registration cost rose from {confirmed_recycle_rao} rao to "
            f"{current_rao} rao between the quote and submission; no TAO was "
            "recycled. Re-run upload to see and confirm the new cost."
        )

    logger.info(
        f"submitting burned_register: hotkey={hotkey_ss58} netuid={netuid} "
        f"recycle={current_rao} rao on subtensor={chain_target}"
    )

    try:
        response = subtensor.burned_register(
            wallet=live_wallet,
            netuid=netuid,
            wait_for_inclusion=True,
            wait_for_finalization=True,
            raise_error=True,
        )
    except Exception as e:
        raise RegistrationSubmissionError(
            f"burned_register extrinsic rejected: {e}"
        ) from e

    if not response.success:
        # raise_error=True should have raised already; defensive.
        raise RegistrationSubmissionError(
            f"burned_register reported failure: {response.message}"
        )

    # The uid is a convenience for the success line. Registration is already
    # final at this point, so a failed lookup must not read as a failed
    # registration.
    try:
        return subtensor.get_uid_for_hotkey_on_subnet(
            hotkey_ss58=hotkey_ss58, netuid=netuid
        )
    except Exception as e:
        logger.debug(f"registered but uid lookup failed: {e!r}")
        return None


def _connect_subtensor(chain_target: str) -> bittensor.Subtensor:
    """Construct a Subtensor client and translate connection failures."""
    import bittensor

    try:
        return bittensor.Subtensor(network=chain_target)
    except Exception as e:
        raise RegistrationSubmissionError(
            f"could not connect to subtensor {chain_target!r}: {e}"
        ) from e
