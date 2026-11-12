# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["async-substrate-interface>=1.5"]
# ///
"""Read-only SN118 parameter snapshot from the public Finney RPC.

EXPERIMENT ONLY. No wallet, no signing, no extrinsics: it reads a handful of
SubtensorModule storage items at the finalized head and prints JSON. The
design doc (docs/design/decentralized-encrypted-infra.md) uses it for the UID
cap, consensus mode (Yuma or Null), mechanism count, registration routes and
burn, and pool price.
"""

from __future__ import annotations

import json

from async_substrate_interface.sync_substrate import SubstrateInterface

RPC = "wss://entrypoint-finney.opentensor.ai:443"
NETUID = 118
RAO = 1e9
ITEMS = (
    "MaxAllowedUids",
    "SubnetworkN",
    "MaxAllowedValidators",
    "Burn",
    "MinBurn",
    "ImmunityPeriod",
    "Tempo",
    "SubnetTAO",
    "SubnetAlphaIn",
    "SubnetAlphaOutEmission",
    "SubnetTaoInEmission",
    "SubnetEpochConsensus",
    "MechanismCountCurrent",
    "MechanismEmissionSplit",
    "NetworkRegistrationAllowed",
    "NetworkPowRegistrationAllowed",
    "BurnIncreaseMult",
    "BurnHalfLife",
)


def main() -> None:
    substrate = SubstrateInterface(url=RPC)
    try:
        head = substrate.get_chain_finalised_head()
        block = substrate.get_block_number(head)
        raw = {}
        for item in ITEMS:
            value = substrate.query("SubtensorModule", item, [NETUID], block_hash=head)
            raw[item] = getattr(value, "value", value)
    finally:
        # An open websocket keeps the interpreter alive after main returns.
        substrate.close()
    spot = raw["SubnetTAO"] / raw["SubnetAlphaIn"]
    per_block = raw["SubnetAlphaOutEmission"] / RAO
    miner_per_day = 0.41 * per_block * 7200
    derived = {
        "registration_burn_tao": raw["Burn"] / RAO,
        "pool_spot_tao_per_alpha": spot,
        "alpha_emission_per_block": per_block,
        "miner_alpha_per_day_if_fully_released": miner_per_day,
        "miner_tao_per_day_at_spot": miner_per_day * spot,
        # U64F64 fixed point; 1.26 means each burn registration raises the price 26%.
        "burn_increase_mult": raw["BurnIncreaseMult"]["bits"] / 2**64,
        "assumptions": "7200 blocks/day, 41% miner share, spot ignores slippage",
    }
    snapshot = {
        "experiment": "sn473-sn118-chain-snapshot",
        "rpc": RPC,
        "finalized_block": block,
        "raw": raw,
        "derived": derived,
    }
    print(json.dumps(snapshot, indent=2))


if __name__ == "__main__":
    main()
