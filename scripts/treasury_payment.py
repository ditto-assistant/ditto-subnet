"""Operator CLI for the isolated SN118 treasury signer journal.

All payment inputs arrive through reviewed JSON files. ``execute`` is the
only command that can load a key or submit a transaction, and it requires the
isolated host, exact confirmation, an unpaused journal, and independent review.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from ditto.treasury.execution import execute_one_leg, validate_instructions
from ditto.treasury.preflight import TopUpBounds, TopUpIntent
from ditto.treasury.store import PaymentPlan, TreasuryStore


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed


def _plan(path: Path) -> PaymentPlan:
    body = json.loads(path.read_text())
    intent = body.pop("intent")
    intent["quoted_at"] = _time(intent["quoted_at"])
    body["instructions_observed_at"] = _time(body["instructions_observed_at"])
    return PaymentPlan(intent=TopUpIntent(**intent), **body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    for command in ("pause", "unpause"):
        control = sub.add_parser(command)
        control.add_argument("--operator", required=True)
        control.add_argument("--reason", required=True)
        if command == "unpause":
            control.add_argument("--reviewer", required=True)
    propose = sub.add_parser("propose")
    propose.add_argument("--plan-file", type=Path, required=True)
    propose.add_argument("--bounds-file", type=Path, required=True)
    propose.add_argument("--instructions-file", type=Path, required=True)
    allocation = sub.add_parser("allocate")
    allocation.add_argument("--allocation-file", type=Path, required=True)
    approve = sub.add_parser("approve")
    approve.add_argument("--key", required=True)
    approve.add_argument("--plan-hash", required=True)
    approve.add_argument("--reviewer", required=True)
    next_leg = sub.add_parser("approve-next")
    next_leg.add_argument("--key", required=True)
    next_leg.add_argument("--plan-hash", required=True)
    next_leg.add_argument("--reviewer", required=True)
    next_leg.add_argument("--instructions-file", type=Path, required=True)
    next_leg.add_argument("--quote-block-hash", required=True)
    next_leg.add_argument("--quote-observed-at", required=True)
    execute = sub.add_parser("execute")
    execute.add_argument("--key", required=True)
    execute.add_argument("--project", required=True)
    execute.add_argument("--instructions-file", type=Path, required=True)
    execute.add_argument("--confirmation", required=True)
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--key", required=True)
    reconcile.add_argument("--before-nano-usd", type=int, required=True)
    reconcile.add_argument("--after-nano-usd", type=int, required=True)
    reconcile.add_argument("--deposit-nano-usd", type=int, required=True)
    reconcile.add_argument("--intervening-usage-nano-usd", type=int, required=True)
    reconcile.add_argument("--deposit-reference", required=True)
    reconcile.add_argument("--reviewer", required=True)
    cancel = sub.add_parser("cancel")
    cancel.add_argument("--key", required=True)
    cancel.add_argument("--operator", required=True)
    cancel.add_argument("--reason", required=True)
    args = parser.parse_args()
    store = TreasuryStore(args.database)
    now = datetime.now(UTC)
    if args.command == "status":
        print(json.dumps(store.status(), sort_keys=True))
    elif args.command in ("pause", "unpause"):
        store.set_pause(
            args.command == "pause",
            operator=args.operator,
            reviewer=getattr(args, "reviewer", None),
            reason=args.reason,
            now=now,
        )
        print(json.dumps(store.status(), sort_keys=True))
    elif args.command == "propose":
        plan = _plan(args.plan_file)
        validate_instructions(args.instructions_file.read_bytes(), asdict(plan))
        bounds = TopUpBounds(**json.loads(args.bounds_file.read_text()))
        print(store.create_plan(plan, bounds, now=now))
    elif args.command == "allocate":
        body = json.loads(args.allocation_file.read_text())
        store.record_allocation(**body, now=now)
    elif args.command == "approve":
        store.approve(
            args.key, reviewer=args.reviewer, expected_plan_hash=args.plan_hash, now=now
        )
    elif args.command == "approve-next":
        plan = store.plan(args.key)
        validate_instructions(args.instructions_file.read_bytes(), plan)
        store.approve_next_leg(
            args.key,
            reviewer=args.reviewer,
            expected_plan_hash=args.plan_hash,
            instructions_sha256=plan["intent"]["payment_instructions_sha256"],
            quote_block_hash=args.quote_block_hash,
            quote_observed_at=_time(args.quote_observed_at),
            now=now,
        )
    elif args.command == "execute":
        if args.confirmation != f"EXECUTE SN118 TREASURY {args.key}":
            parser.error("exact per-payment execution confirmation is required")
        result = execute_one_leg(
            store,
            args.key,
            project=args.project,
            instructions=args.instructions_file.read_bytes(),
            now=now,
        )
        print(json.dumps(asdict(result), sort_keys=True))
    elif args.command == "reconcile":
        store.reconcile_gm(
            args.key,
            before_nano_usd=args.before_nano_usd,
            after_nano_usd=args.after_nano_usd,
            deposit_nano_usd=args.deposit_nano_usd,
            intervening_usage_nano_usd=args.intervening_usage_nano_usd,
            deposit_reference=args.deposit_reference,
            reviewer=args.reviewer,
            now=now,
        )
    elif args.command == "cancel":
        store.cancel(args.key, operator=args.operator, reason=args.reason, now=now)


if __name__ == "__main__":
    main()
