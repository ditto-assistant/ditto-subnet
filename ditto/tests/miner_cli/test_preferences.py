"""Tests for the miner CLI's human-editable local preferences."""

from __future__ import annotations

import json
from pathlib import Path

from ditto.miner_cli.models import PaymentReceipt
from ditto.miner_cli.preferences import (
    clear_pending_payment,
    load_agent_name,
    load_pending_payment,
    load_pending_payments_for_hotkey,
    load_previous_agent_wallet,
    preferences_path,
    save_agent_name,
    save_agent_wallet,
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


def test_agent_wallet_history_returns_latest_different_hotkey(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DITTO_CLI_CONFIG_PATH", str(tmp_path / "config.json"))
    assert save_agent_wallet(
        network="finney",
        name="alpha",
        coldkey_name="cold-a",
        hotkey_name="hot-a",
        hotkey_ss58="5HotA",
    )
    assert save_agent_wallet(
        network="finney",
        name="alpha",
        coldkey_name="cold-b",
        hotkey_name="hot-b",
        hotkey_ss58="5HotB",
    )

    previous = load_previous_agent_wallet(
        network="finney", name="alpha", excluding_hotkey="5HotB"
    )
    assert previous is not None
    assert previous.coldkey_name == "cold-a"
    assert previous.hotkey_name == "hot-a"
    assert previous.hotkey_ss58 == "5HotA"
    assert (
        load_previous_agent_wallet(
            network="test", name="alpha", excluding_hotkey="5HotB"
        )
        is None
    )


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

    discovered = load_pending_payments_for_hotkey(network="finney", hotkey="hk")
    assert len(discovered) == 1
    assert discovered[0].name == "agent"
    assert discovered[0].sha256 == "cd" * 32
    assert discovered[0].payment == payment

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


_WRITER = """
import sys
from ditto.miner_cli.preferences import (
    ReadmissionRecord, save_agent_name, save_readmission_record,
)
worker, rounds = sys.argv[1], int(sys.argv[2])
for i in range(rounds):
    assert save_agent_name(network="finney", hotkey=f"hk-{worker}-{i}", name="n")
    assert save_readmission_record(
        network="finney",
        hotkey=f"hk-{worker}",
        record=ReadmissionRecord(
            agent_id=f"agent-{i}",
            bench_version=19,
            state="paying",
            amount_rao=1,
            send_address="5Dest",
        ),
    )
"""


def test_concurrent_writers_never_erase_a_readmission_record(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """4607477876: every read-modify-write holds one OS lock, so a preference
    write in another process can never overwrite a `paying` guard with a
    stale snapshot."""
    import os
    import subprocess
    import sys

    from ditto.miner_cli.preferences import load_readmission_record

    config = tmp_path / "config.json"
    monkeypatch.setenv("DITTO_CLI_CONFIG_PATH", str(config))
    workers, rounds = 6, 15
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, str(worker), str(rounds)],
            env=os.environ.copy(),
        )
        for worker in range(workers)
    ]
    assert [proc.wait(60) for proc in procs] == [0] * workers
    for worker in range(workers):
        for i in range(rounds):
            assert load_agent_name(network="finney", hotkey=f"hk-{worker}-{i}") == "n"
            record = load_readmission_record(
                network="finney", hotkey=f"hk-{worker}", agent_id=f"agent-{i}"
            )
            assert record is not None and record.state == "paying"
    assert config.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".config.json.*.tmp"))


def test_a_write_never_overwrites_an_unparseable_config(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """A corrupt file may hold a payment record: writers leave it alone."""
    config = tmp_path / "config.json"
    config.write_text('{"pending_readmissions": {')
    monkeypatch.setenv("DITTO_CLI_CONFIG_PATH", str(config))
    assert save_agent_name(network="finney", hotkey="hk", name="x") is False
    assert config.read_text() == '{"pending_readmissions": {'


def test_an_invalid_proof_is_refused_before_it_is_saved(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """4607621003: the writer enforces the loader's proof shape, so it can
    never leave a record the readmission flow cannot read back."""
    import pytest

    from ditto.miner_cli.errors import MinerCliError
    from ditto.miner_cli.preferences import (
        ReadmissionRecord,
        load_readmission_record,
        save_readmission_record,
    )

    monkeypatch.setenv("DITTO_CLI_CONFIG_PATH", str(tmp_path / "config.json"))
    good = ReadmissionRecord(
        agent_id="agent-1",
        bench_version=19,
        state="paying",
        amount_rao=1,
        send_address="5Dest",
    )
    assert save_readmission_record(network="finney", hotkey="hk", record=good)
    bad = ReadmissionRecord(
        agent_id="agent-1",
        bench_version=19,
        state="paid",
        amount_rao=1,
        send_address="5Dest",
        payment=PaymentReceipt(
            block_hash="0x" + "ab" * 32, block_number=77, extrinsic_index=-1
        ),
    )
    with pytest.raises(MinerCliError, match="invalid payment proof"):
        save_readmission_record(network="finney", hotkey="hk", record=bad)
    kept = load_readmission_record(network="finney", hotkey="hk", agent_id="agent-1")
    assert kept is not None and kept.state == "paying"
