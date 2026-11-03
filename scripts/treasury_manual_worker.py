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


def consume_queued(mailbox, journal, policy, chain):
    """Consume at most one request; unknown delivery remains unacknowledged."""
    message = mailbox.pull()
    if message:
        try:
            report = consume_manual(mailbox, journal, policy, chain, *message)
            retry = report is not None and report.status == "pending"
        except Exception as error:
            print(
                json.dumps(
                    {
                        "status": "manual_request_processing_failed",
                        "error_type": type(error).__name__,
                    }
                ),
                flush=True,
            )
            retry = True
        if retry:
            # A transient archive read or unfinalized broadcast used to sit out
            # the 600s ack deadline per attempt, which expired a confirmed
            # request on 2026-10-26. Redelivery reuses the same journal claim.
            release_request(mailbox, message[0])
        publish_current_readiness(mailbox, journal, policy, chain)
        return True
    return False


def release_request(mailbox, ack_id):
    try:
        mailbox.release(ack_id)
    except Exception as error:
        # The lease then simply runs to its deadline, as before.
        print(
            json.dumps(
                {
                    "status": "manual_request_release_failed",
                    "error_type": type(error).__name__,
                }
            ),
            flush=True,
        )


def consume_iteration(mailbox, journal, policy, chain):
    # A confirmed request has a finite signing window. Check between bounded
    # archive chunks too, so a new arrival does not wait for the whole sweep.
    if consume_queued(mailbox, journal, policy, chain):
        return
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
                if consume_queued(mailbox, journal, policy, chain):
                    return
                observe_earning_source(journal, policy, chain, block)
                recent += 1
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
    # No automatic money tick; consume at most one manual request per iteration.
    remaining = 32 - recent
    for index in range(4):
        if index and consume_queued(mailbox, journal, policy, chain):
            return
        cursor = journal.db.execute("SELECT block FROM cursor WHERE id=1").fetchone()[0]
        chunk = min(8, remaining)
        try:
            observe_earnings(journal, policy, chain, max_blocks=chunk)
        except Exception as error:
            # Catch-up rolls back and only adds proved sources later. An
            # archive refusal (e.g. its historical rate limit) must not hide
            # current readiness or strand an already queued request.
            print(
                json.dumps(
                    {
                        "status": "earnings_catchup_unavailable",
                        "error_type": type(error).__name__,
                    }
                ),
                flush=True,
            )
            publish_current_readiness(mailbox, journal, policy, chain)
            break
        remaining -= chunk
        publish_current_readiness(mailbox, journal, policy, chain)
        if (
            journal.db.execute("SELECT block FROM cursor WHERE id=1").fetchone()[0]
            == cursor
            or remaining == 0
        ):
            break


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
