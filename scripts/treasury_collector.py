#!/usr/bin/env python3
"""One bounded tick; default-off signed policy, no generic transaction API."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from ditto.treasury.activity_export import (
    export_finalized_distributions,
    write_selector_snapshot,
)
from ditto.treasury.collector import CollectorJournal, tick
from ditto.treasury.collector_chain import PublicCollectorChain, load_policy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("registration", "transfer"), required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--watch-only", action="store_true")
    parser.add_argument("--initialize-journal", action="store_true")
    parser.add_argument("--export-activity", action="store_true")
    parser.add_argument("--selector-snapshot", type=Path)
    parser.add_argument("--snapshot-only", action="store_true")
    args = parser.parse_args()
    policy = load_policy(args.policy, args.policy_sha256)
    if args.snapshot_only and (not args.selector_snapshot or not args.journal):
        parser.error("snapshot-only requires existing transfer journal and snapshot")
    if args.selector_snapshot is not None and (
        args.role != "transfer"
        or args.initialize_journal
        or args.watch_only
        or args.export_activity
    ):
        parser.error("selector snapshot requires ordinary transfer tick only")
    if args.export_activity:
        if (
            args.role != "transfer"
            or not args.journal
            or args.initialize_journal
            or args.watch_only
        ):
            parser.error("activity export requires existing transfer journal only")
        import bittensor as bt

        with bt.Subtensor(network="archive") as subtensor:
            chain = PublicCollectorChain(subtensor.substrate, role="transfer")

            def epoch_at(block):
                at = subtensor.substrate.get_block_hash(block)
                chain.guard_runtime(policy, at, historical=True)
                return chain.query("SubtensorModule", "SubnetEpochIndex", [118], at)

            print(
                json.dumps(
                    {
                        "selections": export_finalized_distributions(
                            args.journal, policy, epoch_at
                        ),
                        "authority": "none",
                        "submit_with": "record_treasury_receipt",
                    }
                )
            )
        return
    if args.initialize_journal:
        if not args.journal or args.watch_only:
            parser.error("initialization requires journal and no watcher")
        CollectorJournal(args.journal, policy, args.role, initialize=True).close()
        print(json.dumps({"status": "journal_initialized", "policy": policy.digest}))
        return
    if not args.watch_only and not args.snapshot_only and not policy.enabled:
        print(json.dumps({"status": "disabled", "policy": policy.digest}))
        return
    if args.snapshot_only:
        # Explicit observation recovery uses existing custody-owned journal;
        # no chain client, key loading, signing or money tick is called.
        journal = CollectorJournal(args.journal, policy, args.role)
        try:
            write_selector_snapshot(journal.db, args.selector_snapshot, policy)
            print(
                json.dumps({"status": "selector_snapshot_ready", "authority": "none"})
            )
        finally:
            journal.close()
        return
    import bittensor as bt

    # The signed cursor and pending receipts may outlive a full node retention
    # window. Keep every historical identity/runtime/effect check; never skip
    # unavailable history or rebase the journal to the current head. SDK 10.5.0
    # pins the official Finney archive endpoint for this network name.
    with bt.Subtensor(network="archive") as subtensor:
        chain = PublicCollectorChain(
            subtensor.substrate,
            role=args.role,
        )
        if args.watch_only:
            print(
                json.dumps(
                    {
                        "policy": policy.digest,
                        "observation": asdict(chain.observe(policy, args.role)),
                    }
                )
            )
            return
        if not args.journal:
            parser.error("--journal required for signer")
        journal = CollectorJournal(args.journal, policy, args.role)
        try:
            result = tick(journal, policy, chain, args.role)
            if args.selector_snapshot is not None:
                try:
                    write_selector_snapshot(journal.db, args.selector_snapshot, policy)
                except Exception:
                    # Durable money tick is already complete. Do not retry it
                    # to repair observation; expose the independent halt.
                    print(
                        json.dumps(
                            {
                                "status": "selector_snapshot_failed",
                                "signer_status": result,
                                "policy": policy.digest,
                            }
                        )
                    )
                    raise SystemExit(1) from None
            print(json.dumps({"status": result, "policy": policy.digest}))
        finally:
            journal.close()


if __name__ == "__main__":
    main()
