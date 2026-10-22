"""The pin generator refuses inputs that would write a degenerate baseline."""

import pytest

from scripts import pin_runtime_interfaces


async def test_call_baseline_must_be_one_of_the_pins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pin_runtime_interfaces,
        "PINS",
        tuple(p for p in pin_runtime_interfaces.PINS if p[0] != "v482"),
    )
    with pytest.raises(SystemExit, match="TREASURY_CALL_BASELINE v482"):
        await pin_runtime_interfaces.render("wss://unused.invalid")
