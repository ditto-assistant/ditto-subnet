"""Unit tests for :mod:`ditto.miner_cli.confirm`."""

from __future__ import annotations

import builtins
import io

import pytest

from ditto.miner_cli import confirm
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
        monkeypatch.setattr(builtins, "input", lambda: "y")

        confirm_payment(**self._kwargs())

        out = capsys.readouterr().out
        assert "1.5 TAO" in out
        assert "1500000000 rao" in out

    def test_y_answer_case_insensitive(self, monkeypatch) -> None:
        monkeypatch.setattr(builtins, "input", lambda: " Y ")

        confirm_payment(**self._kwargs())

    def test_n_answer_raises_cancelled(self, monkeypatch) -> None:
        monkeypatch.setattr(builtins, "input", lambda: "n")

        with pytest.raises(PaymentCancelledError):
            confirm_payment(**self._kwargs())

    def test_blank_answer_raises_cancelled(self, monkeypatch) -> None:
        """Default-N posture: empty input declines."""
        monkeypatch.setattr(builtins, "input", lambda: "")

        with pytest.raises(PaymentCancelledError):
            confirm_payment(**self._kwargs())

    def test_eof_raises_cancelled(self, monkeypatch) -> None:
        def _raise() -> str:
            raise EOFError

        monkeypatch.setattr(builtins, "input", _raise)

        with pytest.raises(PaymentCancelledError):
            confirm_payment(**self._kwargs())

    def test_skip_bypasses_prompt_entirely(self, monkeypatch, capsys) -> None:
        """--yes path must not call input()."""

        def _raise() -> str:
            raise AssertionError("input() should not have been called")

        monkeypatch.setattr(builtins, "input", _raise)

        confirm_payment(**self._kwargs(skip=True))

        assert "1.5 TAO" in capsys.readouterr().out

    def test_preview_names_the_competition_being_paid(self, capsys) -> None:
        confirm_payment(
            **self._kwargs(skip=True), purpose="generator competition upload fee"
        )
        assert "For:     generator competition upload fee" in capsys.readouterr().out


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
        monkeypatch.setattr(builtins, "input", lambda: "y")

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
        assert "does NOT pay the evaluation fee" in out

    def test_y_answer_is_accepted(self, monkeypatch) -> None:
        monkeypatch.setattr(builtins, "input", lambda: "y")

        confirm_registration(quote=self._quote(), skip=False)

    def test_n_answer_raises_cancelled(self, monkeypatch) -> None:
        monkeypatch.setattr(builtins, "input", lambda: "n")

        with pytest.raises(RegistrationCancelledError):
            confirm_registration(quote=self._quote(), skip=False)

    def test_blank_answer_raises_cancelled(self, monkeypatch) -> None:
        monkeypatch.setattr(builtins, "input", lambda: "")

        with pytest.raises(RegistrationCancelledError):
            confirm_registration(quote=self._quote(), skip=False)

    def test_eof_raises_cancelled(self, monkeypatch) -> None:
        def _eof():
            raise EOFError

        monkeypatch.setattr(builtins, "input", _eof)

        with pytest.raises(RegistrationCancelledError, match="EOF"):
            confirm_registration(quote=self._quote(), skip=False)

    def test_skip_still_prints_the_preview(self, monkeypatch, capsys) -> None:
        def _boom():
            raise AssertionError("prompt must not run when skip=True")

        monkeypatch.setattr(builtins, "input", _boom)

        confirm_registration(quote=self._quote(), skip=True)

        assert "0.0005 TAO" in capsys.readouterr().out


@pytest.mark.parametrize(
    "name,kwargs",
    [
        (
            "confirm_payment",
            {
                "amount_rao": 1,
                "dest_address": "fixture",
                "hotkey_ss58": "fixture",
                "coldkey_name": "fixture",
            },
        ),
        ("confirm_registration", {"quote": TestConfirmRegistration()._quote()}),
        (
            "confirm_attestation",
            {
                "netuid": 118,
                "hotkey_lo": "fixture-lo",
                "hotkey_hi": "fixture-hi",
                "lo_key_kind": "hotkey",
                "hi_key_kind": "hotkey",
                "lo_signer": "fixture-lo",
                "hi_signer": "fixture-hi",
            },
        ),
        (
            "confirm_name_action",
            {"action": "claim", "name": "fixture", "hotkey_ss58": "fixture"},
        ),
        (
            "confirm_avatar_action",
            {"action": "set", "hotkey_ss58": "fixture", "detail": None},
        ),
        (
            "confirm_login_action",
            {
                "user_code": "fixture",
                "hotkey_ss58": "fixture",
                "scopes": "read profile",
                "hours": 1,
            },
        ),
    ],
)
def test_all_confirmation_prompts_use_stderr(name, kwargs, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    getattr(confirm, name)(**kwargs, skip=False)
    output = capsys.readouterr()
    assert "[y/N]" in output.err
    assert "[y/N]" not in output.out


class _Recorder(io.StringIO):
    """A piped (block-buffered) stream that records when it is flushed."""

    def __init__(self, name: str, log: list[str]) -> None:
        super().__init__()
        self.name, self.log = name, log

    def write(self, text: str) -> int:
        self.log.append(f"{self.name} write {text!r}")
        return super().write(text)

    def flush(self) -> None:
        self.log.append(f"{self.name} flush")


def test_prompt_follows_the_flushed_preview(monkeypatch) -> None:
    log: list[str] = []
    monkeypatch.setattr("sys.stdout", _Recorder("stdout", log))
    monkeypatch.setattr("sys.stderr", _Recorder("stderr", log))
    monkeypatch.setattr(builtins, "input", lambda: "y")
    print("preview the miner must see")

    assert confirm.prompt_input("Confirm? ") == "y"

    # The preview reaches the terminal before the prompt that asks about it.
    assert log.index("stdout flush") < log.index("stderr write 'Confirm? '")
