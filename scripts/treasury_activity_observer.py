#!/usr/bin/env python3
"""Default-off bounded public-MCP activity observer; no signer or activation."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from ditto.treasury.activity_observer import (
    ActivityObserverConfig,
    PublicActivityMCP,
    observer_token,
    run_observer,
)
from ditto.treasury.collector_chain import PublicCollectorChain
from ditto.treasury.selector_handoff import SelectorHandoff
from ditto_screening_protocol.collector_receipts import (
    AUDITED_COLLECTOR_CODE_HASH,
    chain_uint,
    collector_receipt_runtime,
)
from ditto_screening_protocol.treasury_approval import TreasuryPolicyApproval


class FinalizedActivityReader:
    def __init__(self, substrate, policy):
        self.substrate = substrate
        self.policy = SimpleNamespace(
            genesis_hash=policy.genesis_hash,
            runtime_code_hash=AUDITED_COLLECTOR_CODE_HASH,
        )
        self.adapter = PublicCollectorChain(substrate, role="transfer")

    def block_hash(self, block):
        return self.substrate.get_block_hash(block)

    def finalized_height(self):
        at = self.substrate.get_chain_finalised_head()
        height = chain_uint(self.substrate.get_block_number(at))
        if self.block_hash(height) != at:
            raise ValueError("observer finalized hash inconsistent")
        return height

    def epoch_at(self, block):
        at = self.block_hash(block)
        self.adapter.guard_runtime(self.policy, at, historical=True)
        return chain_uint(
            self.adapter.query("SubtensorModule", "SubnetEpochIndex", [118], at)
        )

    def finalized_payment_block(self, block):
        if not 0 < block <= self.finalized_height():
            raise ValueError("observer block not finalized")
        at = self.block_hash(block)
        codes = [
            self.adapter.guard_runtime(self.policy, pinned, historical=True)
            for pinned in (self.block_hash(block - 1), at)
        ]
        collector_receipt_runtime(*codes)
        raw = self.substrate.rpc_request("chain_getBlock", [at])
        encoded = raw["result"]["block"]["extrinsics"]
        events = self.substrate.get_events(at)
        if (
            not isinstance(encoded, list)
            or len(encoded) > 4096
            or not isinstance(events, list)
            or len(events) > 8192
            or any(not isinstance(e, dict) for e in events)
        ):
            raise ValueError("observer canonical block unavailable")
        return at, self.epoch_at(block), events, encoded


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--transfer-journal", type=Path)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--initialize-selector-state", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args()
    raw = args.config.read_bytes()
    if (
        hashlib.sha256(raw).hexdigest() != args.config_sha256
        or not 15 <= args.poll_seconds <= 600
    ):
        parser.error("immutable observer config or bounded poll interval invalid")
    body = json.loads(raw)
    if not isinstance(body, dict):
        parser.error("configuration must be an object")
    handoff = body.get("selector_handoff")
    if "selector_handoff" in body:
        try:
            handoff = SelectorHandoff.from_mapping(handoff)
        except (TypeError, ValueError) as exc:
            parser.error(str(exc))
    config = ActivityObserverConfig(
        approval=TreasuryPolicyApproval.model_validate(body["approval"]),
        settings_checksum=body["settings_checksum"],
        start_block=body["start_block"],
        enabled=body.get("enabled", False),
        max_blocks=body.get("max_blocks", 16),
        max_deliveries=body.get("max_deliveries", 100),
        selector_handoff=handoff,
    )
    if not config.enabled:
        print(json.dumps({"status": "disabled", "authority": "none"}))
        return
    if args.state is None:
        parser.error("approved observer activation requires private state path")
    if args.initialize_selector_state and (not args.once or handoff is None):
        parser.error("selector initialization requires handoff and --once")
    if handoff is not None and args.transfer_journal is not None:
        parser.error("selector watcher cannot read the private journal")
    # Existing approved binding only. This program cannot mint/refresh OAuth,
    # read desktop credentials or install a token/secret on any host.
    token = observer_token(
        args.token_file,
        environment_token=os.environ.get("BACKROOM_ACTIVITY_OBSERVER_TOKEN", ""),
    )
    import bittensor as bt

    with bt.Subtensor(network="finney") as subtensor:
        reader = FinalizedActivityReader(subtensor.substrate, config.approval.policy)
        run_observer(
            config,
            state_path=args.state,
            transfer_journal=args.transfer_journal,
            chain=reader,
            mcp_factory=lambda: PublicActivityMCP(token),
            poll_seconds=args.poll_seconds,
            once=args.once,
            emit=lambda result: print(json.dumps(result), flush=True),
            initialize_selector_state=args.initialize_selector_state,
        )


if __name__ == "__main__":
    main()
