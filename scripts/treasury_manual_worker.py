#!/usr/bin/env python3
"""Explicitly staged manual-only consumer. No recurring distribution tick."""

import argparse
import json
import time

from ditto.treasury.collector import CollectorJournal, observe_earnings
from ditto.treasury.collector_chain import PublicCollectorChain, load_policy
from ditto.treasury.manual_worker import process_manual, publish_readiness
from ditto_screening_protocol.treasury_pubsub import TreasuryMailbox


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--request-subscription", required=True)
    parser.add_argument("--report-topic", required=True)
    parser.add_argument("--enabled", action="store_true")
    args = parser.parse_args()
    if not args.enabled:
        print(json.dumps({"status": "disabled"}))
        return
    from pathlib import Path

    import bittensor as bt

    policy = load_policy(Path(args.policy), args.policy_sha256)
    if not policy.enabled or policy.gcp_project != args.project:
        parser.error("enabled immutable custody project pin required")
    mailbox = TreasuryMailbox(
        project=args.project,
        topic=args.report_topic,
        subscription=args.request_subscription,
    )
    # One process owns the existing journal for its lifetime. Normal automatic
    # collector timers must remain stopped, not run alongside this consumer.
    journal = CollectorJournal(Path(args.journal), policy, "transfer")
    try:
        while True:
            try:
                with bt.Subtensor(network="archive") as subtensor:
                    chain = PublicCollectorChain(subtensor.substrate, role="transfer")
                    # Advance proved earnings only; no automatic money tick.
                    observe_earnings(journal, policy, chain)
                    publish_readiness(mailbox, journal, policy, chain)
                    message = mailbox.pull()
                    if message:
                        process_manual(mailbox, journal, policy, chain, *message)
            except Exception as error:
                # No payloads, tokens, signed bytes or raw provider errors.
                print(
                    json.dumps(
                        {
                            "status": "manual_bridge_blocked",
                            "error_type": type(error).__name__,
                        }
                    ),
                    flush=True,
                )
            time.sleep(30)
    finally:
        journal.close()


if __name__ == "__main__":
    main()
