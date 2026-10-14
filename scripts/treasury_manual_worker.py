#!/usr/bin/env python3
"""Explicitly staged manual-only consumer. No recurring distribution tick."""

import argparse
import contextlib
import json
import time

from ditto.treasury.collector import (
    CollectorJournal,
    observe_earning_source,
    observe_earnings,
)
from ditto.treasury.collector_chain import PublicCollectorChain, load_policy
from ditto.treasury.manual_worker import (
    consume_manual,
    publish_observation_blocker,
    publish_readiness,
)
from ditto_screening_protocol.treasury_pubsub import TreasuryMailbox


def consume_iteration(mailbox, journal, policy, chain):
    recent = 0
    if getattr(chain, "manual_approval", None) is not None:
        try:
            proved = {r[0] for r in journal.db.execute("SELECT block FROM earnings")}
            probed = {
                json.loads(r[0])["block"]
                for r in journal.db.execute(
                    "SELECT payload FROM events WHERE event='manual_source_probed' "
                    "AND json_extract(payload,'$.policy')=?",
                    (policy.digest,),
                )
            }
            for block in chain.recent_earning_candidates(policy):
                if block in proved or block in probed:
                    continue
                recent += 1
                observe_earning_source(journal, policy, chain, block)
                publish_current_readiness(mailbox, journal, policy, chain)
                if recent == 8:
                    break
        except Exception as error:
            print(
                json.dumps(
                    {
                        "status": "recent_source_proof_unavailable",
                        "error_type": type(error).__name__,
                    }
                ),
                flush=True,
            )
    # Keep the existing total bound of 32 proved blocks, committing eight at
    # a time so current custody readiness is not starved by archive catch-up.
    # No automatic money tick; still pull only one manual message below.
    remaining = 32 - recent
    for _ in range(4):
        cursor = journal.db.execute("SELECT block FROM cursor WHERE id=1").fetchone()[0]
        chunk = min(8, remaining)
        observe_earnings(journal, policy, chain, max_blocks=chunk)
        remaining -= chunk
        publish_current_readiness(mailbox, journal, policy, chain)
        if (
            journal.db.execute("SELECT block FROM cursor WHERE id=1").fetchone()[0]
            == cursor
            or remaining == 0
        ):
            break
    message = mailbox.pull()
    if message:
        try:
            consume_manual(mailbox, journal, policy, chain, *message)
        except Exception as error:
            # Leave the exact message unacknowledged for normal reconciliation;
            # a per-request failure does not invalidate the observed readiness.
            print(
                json.dumps(
                    {
                        "status": "manual_request_processing_failed",
                        "error_type": type(error).__name__,
                    }
                ),
                flush=True,
            )


def publish_current_readiness(mailbox, journal, policy, chain):
    try:
        publish_readiness(mailbox, journal, policy, chain)
    except Exception as error:
        # Readiness is an observation; existing exact requests still undergo
        # independent custody validation and durable unknown-delivery handling.
        print(
            json.dumps(
                {
                    "status": "readiness_publish_failed",
                    "error_type": type(error).__name__,
                }
            ),
            flush=True,
        )
        with contextlib.suppress(Exception):
            publish_observation_blocker(mailbox, policy, error)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--request-subscription", required=True)
    parser.add_argument("--report-topic", required=True)
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument("--manual-approval")
    parser.add_argument("--manual-approval-sha256")
    args = parser.parse_args()
    if not args.enabled:
        print(json.dumps({"status": "disabled"}))
        return
    from pathlib import Path

    import bittensor as bt

    policy = load_policy(Path(args.policy), args.policy_sha256)
    if not policy.enabled or policy.gcp_project != args.project:
        parser.error("enabled immutable custody project pin required")
    if bool(args.manual_approval) != bool(args.manual_approval_sha256):
        parser.error("manual approval file and immutable digest must be paired")
    manual_approval = None
    if args.manual_approval:
        from ditto_screening_protocol.treasury_manual_limit import ManualLimitApproval

        manual_approval = ManualLimitApproval.model_validate_json(
            Path(args.manual_approval).read_text()
        )
        manual_approval.verify(policy.digest, policy.collector_coldkey)
        if manual_approval.policy.digest != args.manual_approval_sha256:
            parser.error("manual approval digest differs from deployment pin")
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
                    chain.manual_approval = manual_approval
                    consume_iteration(mailbox, journal, policy, chain)
            except Exception as error:
                with contextlib.suppress(Exception):
                    publish_observation_blocker(mailbox, policy, error)
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
