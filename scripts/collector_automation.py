#!/usr/bin/env python3
"""One bounded tick; default-off signed policy, no generic transaction API."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

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
    args = parser.parse_args()
    policy = load_policy(args.policy, args.policy_sha256)
    if args.initialize_journal:
        if not args.journal or args.watch_only:
            parser.error("initialization requires journal and no watcher")
        CollectorJournal(args.journal, policy, args.role, initialize=True).close()
        print(json.dumps({"status": "journal_initialized", "policy": policy.digest}))
        return
    if not args.watch_only and not policy.enabled:
        print(json.dumps({"status": "disabled", "policy": policy.digest}))
        return
    import bittensor as bt

    with bt.Subtensor(network="finney") as subtensor:
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
            print(json.dumps({"status": result, "policy": policy.digest}))
        finally:
            journal.close()


if __name__ == "__main__":
    main()
