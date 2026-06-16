"""Unit tests for :mod:`ditto.miner_cli.confirm`."""

from __future__ import annotations

import builtins

import pytest

from ditto.miner_cli.confirm import confirm_payment
from ditto.miner_cli.errors import PaymentCancelledError


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

        result = confirm_payment(**self._kwargs())

        assert result is None
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
