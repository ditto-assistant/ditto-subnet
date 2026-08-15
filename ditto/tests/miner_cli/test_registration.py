"""Unit tests for :mod:`ditto.miner_cli.registration`.

The bittensor SDK is mocked at the module boundary exactly as in
:mod:`ditto.tests.miner_cli.test_payment`: the real Subtensor connects
over the network and a unit test must not.

Invariants pinned:

- Subtensor is constructed with the requested network identifier, and
  ``chain_endpoint`` overrides it
- The recycle cost is always a live chain read, never a constant
- An already-registered hotkey raises rather than recycling TAO, which
  is what protects a CLI/platform netuid mismatch from costing money
- A missing subnet, an unreadable cost, and an unreadable balance each
  fail closed before submission
- An unaffordable quote is returned (not raised) so the caller can show
  the shortfall
- The cost is re-read immediately before submission and a rise above the
  confirmed amount aborts without submitting
- A cost that FALLS between quote and submission still proceeds
- uid lookup failure after a successful register is cosmetic, not a
  failed registration
"""

from __future__ import annotations

from unittest.mock import MagicMock

import bittensor
import pytest

from ditto.miner_cli.errors import (
    RegistrationNotNeededError,
    RegistrationOutcomeUnknownError,
    RegistrationSubmissionError,
)
from ditto.miner_cli.registration import quote_registration, submit_registration

HOTKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
COLDKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
NETUID = 118


def _wallet(signer: str = COLDKEY) -> MagicMock:
    wallet = MagicMock()
    wallet.coldkeypub.ss58_address = signer
    return wallet


def _subtensor(
    *,
    exists: bool = True,
    registered: bool = False,
    recycle_rao: int = 500_000,
    balance_rao: int = 10_000_000_000,
) -> MagicMock:
    subtensor = MagicMock()
    subtensor.subnet_exists.return_value = exists
    subtensor.is_hotkey_registered.return_value = registered
    subtensor.recycle.return_value = bittensor.Balance.from_rao(recycle_rao)
    subtensor.get_balance.return_value = bittensor.Balance.from_rao(balance_rao)
    return subtensor


class TestQuoteRegistration:
    def test_reads_live_cost_and_balance(self, monkeypatch) -> None:
        subtensor = _subtensor(recycle_rao=500_000, balance_rao=12_402_100_000)
        ctor = MagicMock(return_value=subtensor)
        monkeypatch.setattr(bittensor, "Subtensor", ctor)

        quote = quote_registration(
            live_wallet=_wallet(),
            hotkey_ss58=HOTKEY,
            coldkey_name="miner",
            netuid=NETUID,
            subtensor_network="finney",
        )

        ctor.assert_called_once_with(network="finney")
        subtensor.recycle.assert_called_once_with(netuid=NETUID)
        subtensor.get_balance.assert_called_once_with(COLDKEY)
        assert quote.recycle_rao == 500_000
        assert quote.balance_rao == 12_402_100_000
        assert quote.netuid == NETUID
        assert quote.hotkey_ss58 == HOTKEY
        assert quote.coldkey_name == "miner"
        assert quote.affordable

    def test_chain_endpoint_overrides_network(self, monkeypatch) -> None:
        ctor = MagicMock(return_value=_subtensor())
        monkeypatch.setattr(bittensor, "Subtensor", ctor)

        quote_registration(
            live_wallet=_wallet(),
            hotkey_ss58=HOTKEY,
            coldkey_name="miner",
            netuid=NETUID,
            subtensor_network="finney",
            chain_endpoint="ws://127.0.0.1:9944",
        )

        ctor.assert_called_once_with(network="ws://127.0.0.1:9944")

    def test_already_registered_refuses_to_burn(self, monkeypatch) -> None:
        subtensor = _subtensor(registered=True)
        monkeypatch.setattr(bittensor, "Subtensor", MagicMock(return_value=subtensor))

        with pytest.raises(RegistrationNotNeededError) as exc:
            quote_registration(
                live_wallet=_wallet(),
                hotkey_ss58=HOTKEY,
                coldkey_name="miner",
                netuid=NETUID,
                subtensor_network="finney",
            )

        message = str(exc.value)
        assert "already registered" in message
        assert "netuid" in message
        assert "No TAO was recycled" in message
        subtensor.recycle.assert_not_called()

    def test_missing_subnet_fails_closed(self, monkeypatch) -> None:
        subtensor = _subtensor(exists=False)
        monkeypatch.setattr(bittensor, "Subtensor", MagicMock(return_value=subtensor))

        with pytest.raises(RegistrationSubmissionError, match="does not exist"):
            quote_registration(
                live_wallet=_wallet(),
                hotkey_ss58=HOTKEY,
                coldkey_name="miner",
                netuid=NETUID,
                subtensor_network="finney",
            )

        subtensor.is_hotkey_registered.assert_not_called()

    def test_unreadable_cost_fails_closed(self, monkeypatch) -> None:
        subtensor = _subtensor()
        subtensor.recycle.side_effect = RuntimeError("boom")
        monkeypatch.setattr(bittensor, "Subtensor", MagicMock(return_value=subtensor))

        with pytest.raises(RegistrationSubmissionError, match="unknown price"):
            quote_registration(
                live_wallet=_wallet(),
                hotkey_ss58=HOTKEY,
                coldkey_name="miner",
                netuid=NETUID,
                subtensor_network="finney",
            )

    def test_none_cost_fails_closed(self, monkeypatch) -> None:
        subtensor = _subtensor()
        subtensor.recycle.return_value = None
        monkeypatch.setattr(bittensor, "Subtensor", MagicMock(return_value=subtensor))

        with pytest.raises(RegistrationSubmissionError, match="unknown price"):
            quote_registration(
                live_wallet=_wallet(),
                hotkey_ss58=HOTKEY,
                coldkey_name="miner",
                netuid=NETUID,
                subtensor_network="finney",
            )

    def test_unreadable_balance_fails_closed(self, monkeypatch) -> None:
        subtensor = _subtensor()
        subtensor.get_balance.side_effect = RuntimeError("rpc down")
        monkeypatch.setattr(bittensor, "Subtensor", MagicMock(return_value=subtensor))

        with pytest.raises(RegistrationSubmissionError, match="free balance"):
            quote_registration(
                live_wallet=_wallet(),
                hotkey_ss58=HOTKEY,
                coldkey_name="miner",
                netuid=NETUID,
                subtensor_network="finney",
            )

    def test_unaffordable_is_returned_not_raised(self, monkeypatch) -> None:
        subtensor = _subtensor(recycle_rao=5_000_000_000, balance_rao=1_000_000_000)
        monkeypatch.setattr(bittensor, "Subtensor", MagicMock(return_value=subtensor))

        quote = quote_registration(
            live_wallet=_wallet(),
            hotkey_ss58=HOTKEY,
            coldkey_name="miner",
            netuid=NETUID,
            subtensor_network="finney",
        )

        assert not quote.affordable

    def test_connection_failure_is_translated(self, monkeypatch) -> None:
        monkeypatch.setattr(
            bittensor, "Subtensor", MagicMock(side_effect=RuntimeError("no route"))
        )

        with pytest.raises(RegistrationSubmissionError, match="could not connect"):
            quote_registration(
                live_wallet=_wallet(),
                hotkey_ss58=HOTKEY,
                coldkey_name="miner",
                netuid=NETUID,
                subtensor_network="finney",
            )


class TestSubmitRegistration:
    def _registered_subtensor(self, recycle_rao: int = 500_000) -> MagicMock:
        subtensor = _subtensor(recycle_rao=recycle_rao)
        response = MagicMock()
        response.success = True
        subtensor.burned_register.return_value = response
        subtensor.get_uid_for_hotkey_on_subnet.return_value = 412
        return subtensor

    def test_submits_and_returns_uid(self, monkeypatch) -> None:
        subtensor = self._registered_subtensor()
        ctor = MagicMock(return_value=subtensor)
        monkeypatch.setattr(bittensor, "Subtensor", ctor)
        wallet = _wallet()

        uid = submit_registration(
            live_wallet=wallet,
            hotkey_ss58=HOTKEY,
            netuid=NETUID,
            subtensor_network="finney",
            confirmed_recycle_rao=500_000,
        )

        assert uid == 412
        subtensor.burned_register.assert_called_once_with(
            wallet=wallet,
            netuid=NETUID,
            wait_for_inclusion=True,
            wait_for_finalization=True,
            raise_error=True,
        )

    def test_cost_rise_aborts_before_submitting(self, monkeypatch) -> None:
        subtensor = self._registered_subtensor(recycle_rao=900_000)
        monkeypatch.setattr(bittensor, "Subtensor", MagicMock(return_value=subtensor))

        with pytest.raises(RegistrationSubmissionError) as exc:
            submit_registration(
                live_wallet=_wallet(),
                hotkey_ss58=HOTKEY,
                netuid=NETUID,
                subtensor_network="finney",
                confirmed_recycle_rao=500_000,
            )

        message = str(exc.value)
        assert "rose from 500000 rao to 900000 rao" in message
        assert "no TAO was recycled" in message
        subtensor.burned_register.assert_not_called()

    def test_cost_drop_still_proceeds(self, monkeypatch) -> None:
        subtensor = self._registered_subtensor(recycle_rao=100_000)
        monkeypatch.setattr(bittensor, "Subtensor", MagicMock(return_value=subtensor))

        uid = submit_registration(
            live_wallet=_wallet(),
            hotkey_ss58=HOTKEY,
            netuid=NETUID,
            subtensor_network="finney",
            confirmed_recycle_rao=500_000,
        )

        assert uid == 412
        subtensor.burned_register.assert_called_once()

    def test_extrinsic_rejection_is_translated(self, monkeypatch) -> None:
        subtensor = self._registered_subtensor()
        subtensor.burned_register.side_effect = RuntimeError("insufficient balance")
        monkeypatch.setattr(bittensor, "Subtensor", MagicMock(return_value=subtensor))

        with pytest.raises(RegistrationSubmissionError, match="insufficient balance"):
            submit_registration(
                live_wallet=_wallet(),
                hotkey_ss58=HOTKEY,
                netuid=NETUID,
                subtensor_network="finney",
                confirmed_recycle_rao=500_000,
            )

    def test_unsuccessful_response_is_translated(self, monkeypatch) -> None:
        subtensor = self._registered_subtensor()
        response = MagicMock()
        response.success = False
        response.message = "registration disabled"
        subtensor.burned_register.return_value = response
        monkeypatch.setattr(bittensor, "Subtensor", MagicMock(return_value=subtensor))

        with pytest.raises(RegistrationSubmissionError, match="registration disabled"):
            submit_registration(
                live_wallet=_wallet(),
                hotkey_ss58=HOTKEY,
                netuid=NETUID,
                subtensor_network="finney",
                confirmed_recycle_rao=500_000,
            )

    def test_uid_lookup_failure_is_not_a_failed_registration(self, monkeypatch) -> None:
        subtensor = self._registered_subtensor()
        subtensor.get_uid_for_hotkey_on_subnet.side_effect = RuntimeError("timeout")
        monkeypatch.setattr(bittensor, "Subtensor", MagicMock(return_value=subtensor))

        uid = submit_registration(
            live_wallet=_wallet(),
            hotkey_ss58=HOTKEY,
            netuid=NETUID,
            subtensor_network="finney",
            confirmed_recycle_rao=500_000,
        )

        assert uid is None
        subtensor.burned_register.assert_called_once()


class TestAmbiguousSubmission:
    """A submission error is not proof that nothing was recycled.

    ``burned_register`` waits for finalization, so a timeout or a dropped
    socket can surface after the extrinsic already landed. Reporting that
    as a plain rejection with a retry hint invites paying twice.
    """

    def _subtensor_that_fails_submitting(
        self, *, registered_after: bool | Exception
    ) -> MagicMock:
        subtensor = _subtensor()
        subtensor.burned_register.side_effect = TimeoutError("no finalization")
        if isinstance(registered_after, Exception):
            subtensor.is_hotkey_registered.side_effect = registered_after
        else:
            subtensor.is_hotkey_registered.return_value = registered_after
        return subtensor

    def _submit(self, subtensor, monkeypatch):
        monkeypatch.setattr(bittensor, "Subtensor", MagicMock(return_value=subtensor))
        return submit_registration(
            live_wallet=_wallet(),
            hotkey_ss58=HOTKEY,
            netuid=NETUID,
            subtensor_network="finney",
            confirmed_recycle_rao=500_000,
        )

    def test_extrinsic_landed_despite_the_error_is_not_retryable(
        self, monkeypatch
    ) -> None:
        subtensor = self._subtensor_that_fails_submitting(registered_after=True)

        with pytest.raises(RegistrationOutcomeUnknownError) as exc:
            self._submit(subtensor, monkeypatch)

        message = str(exc.value)
        assert "IS now registered" in message
        assert "TAO was recycled" in message
        assert "Do NOT register again" in message

    def test_unresolvable_state_refuses_to_advise_a_retry(self, monkeypatch) -> None:
        subtensor = self._subtensor_that_fails_submitting(
            registered_after=RuntimeError("rpc down")
        )

        with pytest.raises(RegistrationOutcomeUnknownError) as exc:
            self._submit(subtensor, monkeypatch)

        message = str(exc.value)
        assert "may or may not have been recycled" in message
        assert "Do NOT re-run registration blindly" in message
        assert "btcli subnets show" in message

    def test_confirmed_unregistered_is_a_definite_safe_to_retry_failure(
        self, monkeypatch
    ) -> None:
        subtensor = self._subtensor_that_fails_submitting(registered_after=False)

        with pytest.raises(RegistrationSubmissionError) as exc:
            self._submit(subtensor, monkeypatch)

        message = str(exc.value)
        assert "still unregistered" in message
        assert "no TAO was recycled" in message
        assert "retrying is safe" in message

    def test_non_success_response_is_also_reconciled(self, monkeypatch) -> None:
        """The defensive branch gets the same treatment as an exception."""
        subtensor = _subtensor()
        response = MagicMock()
        response.success = False
        response.message = "unknown"
        subtensor.burned_register.return_value = response
        subtensor.is_hotkey_registered.return_value = True

        monkeypatch.setattr(bittensor, "Subtensor", MagicMock(return_value=subtensor))

        with pytest.raises(RegistrationOutcomeUnknownError, match="IS now registered"):
            submit_registration(
                live_wallet=_wallet(),
                hotkey_ss58=HOTKEY,
                netuid=NETUID,
                subtensor_network="finney",
                confirmed_recycle_rao=500_000,
            )
