from __future__ import annotations

import json
from pathlib import Path

import pytest

from ditto.preview.align import align_engine, hotkeys_from_json
from ditto.preview.engine import IsolationError, PreviewEngine

HOTKEY = "5EexQS8UxChmkZ6vGeacAkwcf3TARR1Go5rd684Mf69dwgTY"
HOTKEY_B = "5G6fGXnXFYdLM3ZyAm9whUbCY4ziQzcbMiTEqZB5c9KekTtR"


def _engine() -> PreviewEngine:
    return PreviewEngine(network="local", endpoint="ws://127.0.0.1:9944", netuid=3)


def test_refuses_finney() -> None:
    with pytest.raises(IsolationError, match="public network"):
        PreviewEngine(network="finney", endpoint="ws://127.0.0.1:9944")
    with pytest.raises(IsolationError, match="opentensor"):
        PreviewEngine(
            network="local",
            endpoint="wss://entrypoint.finney.opentensor.ai:443",
        )


def test_register_permit_warp_and_snapshot() -> None:
    engine = _engine()
    neuron = engine.register(HOTKEY, permit=False, stake=0.5)
    assert neuron.uid == 0
    engine.permit(HOTKEY, True)
    assert engine.neurons[HOTKEY].permit is True
    engine.warp_block(10)
    assert engine.block == 11
    engine.snapshot("clean")
    engine.warp_tempo(1)
    assert engine.block == 11 + engine.tempo
    engine.revert("clean")
    assert engine.block == 11
    assert engine.neurons[HOTKEY].permit is True


def test_lease_expires_on_warp_and_via_cheatcode() -> None:
    engine = _engine()
    engine.register(HOTKEY, permit=True)
    lease = engine.issue_lease(HOTKEY, lifetime_blocks=5)
    assert lease.expired is False
    engine.warp_block(5)
    assert engine.leases[lease.lease_id].expired is True
    second = engine.issue_lease(HOTKEY, lifetime_blocks=100)
    engine.expire_lease(second.lease_id)
    assert engine.leases[second.lease_id].expired is True


def test_allowance_and_provider_faults() -> None:
    engine = _engine()
    grant = engine.issue_grant()
    engine.exhaust_allowance(grant.grant_id)
    assert engine.grants[grant.grant_id].exhausted is True
    engine.inject_provider(429)
    assert engine.provider_status == 429
    engine.inject_provider(None)
    assert engine.provider_status is None
    engine.drop_relay(True)
    assert engine.relay_dropped is True
    with pytest.raises(ValueError):
        engine.inject_provider(500)


def test_align_from_json_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"hotkeys": [HOTKEY, HOTKEY_B, HOTKEY]}))
    engine = _engine()
    aligned = align_engine(engine, json_path=path)
    assert aligned == [HOTKEY, HOTKEY_B]
    assert engine.neurons[HOTKEY].permit is True
    assert engine.neurons[HOTKEY_B].uid == 1
    assert hotkeys_from_json(path) == [HOTKEY, HOTKEY_B]
