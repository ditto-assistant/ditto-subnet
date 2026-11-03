#!/usr/bin/env python3
"""Operator-only unsigned expiry recovery; public chain reads, no signer."""

import argparse
import json
from pathlib import Path

from ditto.treasury.collector import CollectorJournal
from ditto.treasury.collector_chain import PublicCollectorChain, load_policy
from ditto.treasury.manual_expiry import expire_unsigned_manual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()
    if args.confirmation != f"EXPIRE UNSIGNED {args.request_id}":
        parser.error("exact unsigned expiry confirmation required")
    policy = load_policy(args.policy, args.policy_sha256)
    import bittensor as bt

    # Acquires the same exclusive file lock as the worker, without initializing
    # or resetting anything. Stop the consumer before this operator action.
    journal = CollectorJournal(args.journal, policy, "transfer", initialize=False)
    try:
        with bt.Subtensor(network="archive") as subtensor:
            proof = expire_unsigned_manual(
                journal,
                policy,
                PublicCollectorChain(subtensor.substrate, role="transfer"),
                args.request_id,
                reason=args.reason,
            )
        print(
            json.dumps(
                {"status": "expired_unsigned", "spending_authority": "none", **proof}
            )
        )
    finally:
        journal.close()


if __name__ == "__main__":
    main()
