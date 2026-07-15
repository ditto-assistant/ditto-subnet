"""Tests for the miner CLI's human-editable local preferences."""

from __future__ import annotations

import json
from pathlib import Path

from ditto.miner_cli.preferences import (
    load_agent_name,
    preferences_path,
    save_agent_name,
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
