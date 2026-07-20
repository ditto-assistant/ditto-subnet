"""Tests for the miner CLI's human-editable local preferences."""

from __future__ import annotations

import json
from pathlib import Path

from ditto.miner_cli.models import PaymentReceipt
from ditto.miner_cli.preferences import (
    clear_pending_payment,
    load_agent_name,
    load_pending_payment,
    preferences_path,
    save_agent_name,
    save_pending_payment,
)


def test_round_trips_names_per_network_and_hotkey(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    config = tmp_path / "config.json"
    monkeypatch.setenv("DITTO_CLI_CONFIG_PATH", str(config))

    assert preferences_path() == config
    assert load_agent_name(network="finney", hotkey="hk") is None
    assert save_agent_name(network="finney", hotkey="hk", name="Jackie")
    assert save_agent_name(network="test", hotkey="hk", name="Jackie test")
    assert load_agent_name(network="finney", hotkey="hk") == "Jackie"
    assert load_agent_name(network="test", hotkey="hk") == "Jackie test"
    assert config.stat().st_mode & 0o777 == 0o600


def test_explicit_update_replaces_the_local_default(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    config = tmp_path / "config.json"
    monkeypatch.setenv("DITTO_CLI_CONFIG_PATH", str(config))

    assert save_agent_name(network="finney", hotkey="hk", name="Jackie")
    assert save_agent_name(network="finney", hotkey="hk", name="Recall")
    assert load_agent_name(network="finney", hotkey="hk") == "Recall"
    assert json.loads(config.read_text())["agent_names"]["finney:hk"] == "Recall"


def test_malformed_preferences_fail_open(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    config = tmp_path / "config.json"
    config.write_text("not json")
    monkeypatch.setenv("DITTO_CLI_CONFIG_PATH", str(config))

    assert load_agent_name(network="finney", hotkey="hk") is None


def test_pending_payment_round_trip_is_exact_and_private(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    config = tmp_path / "config.json"
    monkeypatch.setenv("DITTO_CLI_CONFIG_PATH", str(config))
    payment = PaymentReceipt(
        block_hash="0x" + "ab" * 32,
        block_number=8_664_060,
        extrinsic_index=24,
    )

    assert save_pending_payment(
        network="finney",
        hotkey="hk",
        name="agent",
        sha256="cd" * 32,
        payment=payment,
    )
    assert (
        load_pending_payment(
            network="finney", hotkey="hk", name="agent", sha256="cd" * 32
        )
        == payment
    )
    assert (
        load_pending_payment(
            network="finney", hotkey="hk", name="agent", sha256="ef" * 32
        )
        is None
    )
    raw = config.read_text()
    assert "tar" not in raw.lower()
    assert config.stat().st_mode & 0o777 == 0o600

    assert clear_pending_payment(
        network="finney",
        hotkey="hk",
        name="agent",
        sha256="cd" * 32,
        payment=payment,
    )
    assert (
        load_pending_payment(
            network="finney", hotkey="hk", name="agent", sha256="cd" * 32
        )
        is None
    )


def test_clear_does_not_remove_a_newer_payment(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DITTO_CLI_CONFIG_PATH", str(tmp_path / "config.json"))
    old = PaymentReceipt("0x" + "ab" * 32, 10, 1)
    new = PaymentReceipt("0x" + "cd" * 32, 11, 2)
    identity = {
        "network": "finney",
        "hotkey": "hk",
        "name": "agent",
        "sha256": "ef" * 32,
    }
    assert save_pending_payment(**identity, payment=new)

    assert clear_pending_payment(**identity, payment=old)
    assert load_pending_payment(**identity) == new
