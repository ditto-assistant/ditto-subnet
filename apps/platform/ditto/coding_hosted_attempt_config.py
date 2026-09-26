"""Explicit one-attempt config materializer; never started by API boot or a unit.

Run on the dedicated host as the worker account with the installed interpreter.
It writes one new owner-only runtime configuration and prints only digests and
fixed paths. It starts nothing and never writes to PostgreSQL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import NoReturn


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise ValueError("hosted attempt config arguments invalid")


def main() -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument(
        "--materialize-attempt-config", action="store_true", required=True
    )
    parser.add_argument("--evaluation-id", required=True)
    parser.add_argument("--runtime-revision", required=True)
    # Each pin must equal what Platform, the policy or the verified input derives.
    parser.add_argument("--assignment-sha256", required=True)
    parser.add_argument("--execution-profile-sha256", required=True)
    parser.add_argument("--grading-profile-sha256", required=True)
    parser.add_argument("--inference-policy-sha256", required=True)
    parser.add_argument("--budget-profile-sha256", required=True)
    parser.add_argument("--probe-receipt-sha256", required=True)
    parser.add_argument("--evidence-wrapping-key-sha256", required=True)
    try:
        args = parser.parse_args()
    except ValueError:
        print(
            "requires --materialize-attempt-config --evaluation-id <uuid> "
            "--runtime-revision <hex> --assignment-sha256 <hex> "
            "--execution-profile-sha256 <hex> --grading-profile-sha256 <hex> "
            "--inference-policy-sha256 <hex> --budget-profile-sha256 <hex> "
            "--probe-receipt-sha256 <hex> --evidence-wrapping-key-sha256 <hex>",
            file=sys.stderr,
        )
        return 2
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    try:
        from ditto.api_server.coding_hosted_attempt_config import (
            AttemptRequest,
            HostedAttemptConfigError,
            materialize_on_host,
        )
    except BaseException:
        print("hosted attempt config unavailable", file=sys.stderr)
        return 1
    try:
        request = AttemptRequest.parse(
            evaluation_id=args.evaluation_id,
            runtime_revision=args.runtime_revision,
            assignment_sha256=args.assignment_sha256,
            execution_profile_sha256=args.execution_profile_sha256,
            grading_profile_sha256=args.grading_profile_sha256,
            inference_policy_sha256=args.inference_policy_sha256,
            budget_profile_sha256=args.budget_profile_sha256,
            probe_receipt_sha256=args.probe_receipt_sha256,
            evidence_wrapping_key_sha256=args.evidence_wrapping_key_sha256,
        )
        receipt = asyncio.run(materialize_on_host(request))
    except HostedAttemptConfigError as error:
        # Stages are fixed strings; no path, value or database detail is attached.
        print(f"hosted attempt config refused: {error}", file=sys.stderr)
        return 1
    except BaseException:
        print(
            "hosted attempt config failed; retain any partial attempt for review",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
