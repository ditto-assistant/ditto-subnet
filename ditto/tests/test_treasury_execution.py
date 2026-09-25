from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import bittensor as bt
import pytest

from ditto.treasury import execution

HASH = "0x" + "a" * 64


class _Chain:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self) -> _Chain:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def get_stake(self, *_args: object) -> bt.Balance:
        return bt.Balance.from_rao(2_000_000_000).set_unit(118)

    def get_balance(self, *_args: object) -> bt.Balance:
        return bt.Balance.from_rao(10_000_000)

    def unstake(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(("unstake", kwargs))
        return self._response(
            {
                "balance_before": bt.Balance.from_rao(1_000_000),
                "balance_after": bt.Balance.from_rao(7_900_000),
            }
        )

    def add_stake(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(("add_stake", kwargs))
        return self._response(
            {
                "stake_before": bt.Balance.from_rao(0).set_unit(28),
                "stake_after": bt.Balance.from_rao(320_000_000).set_unit(28),
            }
        )

    def transfer(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(("transfer", kwargs))
        return self._response({})

    def transfer_stake(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(("transfer_stake", kwargs))
        return self._response({})

    @staticmethod
    def _response(data: dict) -> SimpleNamespace:
        return SimpleNamespace(
            success=True,
            data=data,
            extrinsic_receipt=SimpleNamespace(
                finalized=True, extrinsic_hash=HASH, block_hash=HASH
            ),
        )


def _plan(route: str) -> dict:
    return {
        "intent": {
            "route": route,
            "linked_wallet": "reviewed-wallet",
            "tao_value_rao": 7_000_000,
        },
        "treasury_hotkey": "treasury-hotkey",
        "gm_hotkey": "gm-hotkey",
        "destination_coldkey": "destination-wallet",
        "gm_account_ref": "gm-account",
        "max_slippage_bps": 50,
        "min_tao_proceeds_rao": 6_500_000,
        "min_gm_alpha_rao": 300_000_000,
    }


def test_exact_billing_instructions_digest() -> None:
    plan = _plan("gm_alpha")
    body = {
        "asset": "gm_alpha",
        "source": "reviewed-wallet",
        "destination": "destination-wallet",
        "hotkey": "gm-hotkey",
        "account_ref": "gm-account",
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    plan["intent"]["payment_instructions_sha256"] = hashlib.sha256(raw).hexdigest()
    execution.validate_instructions(raw, plan)
    body["destination"] = "attacker-wallet"
    with pytest.raises(ValueError, match="differ"):
        execution.validate_instructions(json.dumps(body).encode(), plan)


def test_both_chain_routes_use_safe_sdk_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    chain = _Chain()
    monkeypatch.setattr(execution.bt, "Subtensor", lambda **_kwargs: chain)
    wallet = SimpleNamespace(coldkeypub=SimpleNamespace(ss58_address="reviewed-wallet"))
    tao = _plan("tao")
    result = execution.dispatch_chain_leg("unstake", 1_000_000_000, tao, wallet)
    assert result.next_amount_rao == 6_900_000
    assert chain.calls[-1][1]["safe_unstaking"] is True
    assert chain.calls[-1][1]["rate_tolerance"] == 0.005
    assert (
        execution.dispatch_chain_leg(
            "deposit_tao", 6_900_000, tao, wallet
        ).next_amount_rao
        is None
    )
    assert chain.calls[-1][1]["destination_ss58"] == "destination-wallet"
    gm = _plan("gm_alpha")
    assert (
        execution.dispatch_chain_leg("stake_gm", 6_900_000, gm, wallet).next_amount_rao
        == 320_000_000
    )
    assert chain.calls[-1][1]["safe_staking"] is True
    assert (
        execution.dispatch_chain_leg(
            "deposit_gm", 320_000_000, gm, wallet
        ).next_amount_rao
        is None
    )
    assert chain.calls[-1][1]["origin_netuid"] == 28
    assert chain.calls[-1][1]["destination_netuid"] == 28
    assert chain.calls[-1][1]["hotkey_ss58"] == "gm-hotkey"
