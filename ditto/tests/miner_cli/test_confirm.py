"""Unit tests for :mod:`ditto.miner_cli.confirm`."""

from __future__ import annotations

import builtins

import pytest

from ditto.miner_cli.confirm import confirm_payment, confirm_registration
from ditto.miner_cli.errors import PaymentCancelledError, RegistrationCancelledError
from ditto.miner_cli.models import RegistrationQuote


class TestConfirmPayment:
    def _kwargs(self, *, skip: bool = False) -> dict:
        return {
            "amount_rao": 1_500_000_000,
            "dest_address": "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
            "hotkey_ss58": "5HpG9w8U" + "x" * 40,
            "coldkey_name": "miner",
            "skip": skip,
        }

    def test_y_answer_returns_none(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(builtins, "input", lambda _: "y")

        # confirm_payment returns None on success; here we just need to
        # know it did not raise (PaymentCancelledError).
        confirm_payment(**self._kwargs())

        out = capsys.readouterr().out
        # Preview is on stdout for user-facing display.
        assert "1.5 TAO" in out
        assert "1500000000 rao" in out

    def test_y_answer_case_insensitive(self, monkeypatch) -> None:
        monkeypatch.setattr(builtins, "input", lambda _: " Y ")

        confirm_payment(**self._kwargs())

    def test_n_answer_raises_cancelled(self, monkeypatch) -> None:
        monkeypatch.setattr(builtins, "input", lambda _: "n")

        with pytest.raises(PaymentCancelledError):
            confirm_payment(**self._kwargs())

    def test_blank_answer_raises_cancelled(self, monkeypatch) -> None:
        """Default-N posture: empty input declines."""
        monkeypatch.setattr(builtins, "input", lambda _: "")

        with pytest.raises(PaymentCancelledError):
            confirm_payment(**self._kwargs())

    def test_eof_raises_cancelled(self, monkeypatch) -> None:
        def _raise(_prompt: str) -> str:
            raise EOFError

        monkeypatch.setattr(builtins, "input", _raise)

        with pytest.raises(PaymentCancelledError):
            confirm_payment(**self._kwargs())

    def test_skip_bypasses_prompt_entirely(self, monkeypatch, capsys) -> None:
        """--yes path must not call input()."""

        def _raise(_prompt: str) -> str:
            raise AssertionError("input() should not have been called")

        monkeypatch.setattr(builtins, "input", _raise)

        confirm_payment(**self._kwargs(skip=True))

        # Preview still printed so the miner sees what they paid.
        assert "1.5 TAO" in capsys.readouterr().out


class TestConfirmRegistration:
    """The recycle amount is burned outright, so the preview must be exact."""

    def _quote(self, recycle_rao: int = 500_000) -> RegistrationQuote:
        return RegistrationQuote(
            netuid=118,
            hotkey_ss58="5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
            coldkey_name="miner",
            coldkey_ss58="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            recycle_rao=recycle_rao,
            balance_rao=12_402_100_000,
        )

    def test_preview_shows_live_cost_in_tao_and_rao(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(builtins, "input", lambda _: "y")

        confirm_registration(quote=self._quote(), skip=False)

        out = capsys.readouterr().out
        assert "0.0005 TAO" in out
        assert "500000 rao" in out
        assert "12.4021 TAO" in out
        assert "Netuid:   118" in out

    def test_preview_states_the_tao_is_burned_not_transferred(self, capsys) -> None:
        confirm_registration(quote=self._quote(), skip=True)

        out = capsys.readouterr().out
        assert "burned, not transferred" in out
        assert "cannot be refunded" in out
        # And that it is not the eval fee, which is confirmed separately.
        assert "does NOT pay the evaluation fee" in out

    def test_y_answer_is_accepted(self, monkeypatch) -> None:
        monkeypatch.setattr(builtins, "input", lambda _: "y")

        # confirm_registration returns None on success; here we just need to
        # know it did not raise (RegistrationCancelledError).
        confirm_registration(quote=self._quote(), skip=False)

    def test_n_answer_raises_cancelled(self, monkeypatch) -> None:
        monkeypatch.setattr(builtins, "input", lambda _: "n")

        with pytest.raises(RegistrationCancelledError):
            confirm_registration(quote=self._quote(), skip=False)

    def test_blank_answer_raises_cancelled(self, monkeypatch) -> None:
        monkeypatch.setattr(builtins, "input", lambda _: "")

        with pytest.raises(RegistrationCancelledError):
            confirm_registration(quote=self._quote(), skip=False)

    def test_eof_raises_cancelled(self, monkeypatch) -> None:
        def _eof(_):
            raise EOFError

        monkeypatch.setattr(builtins, "input", _eof)

        with pytest.raises(RegistrationCancelledError, match="EOF"):
            confirm_registration(quote=self._quote(), skip=False)

    def test_skip_still_prints_the_preview(self, monkeypatch, capsys) -> None:
        def _boom(_):
            raise AssertionError("prompt must not run when skip=True")

        monkeypatch.setattr(builtins, "input", _boom)

        confirm_registration(quote=self._quote(), skip=True)

        # The miner still sees what was recycled on their behalf.
        assert "0.0005 TAO" in capsys.readouterr().out
