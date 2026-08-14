"""``ditto logs``: read your own agent's harness diagnostics, self-serve.

When a submission fails scoring, the ticket ledger says only *how* the platform
responded -- ``infrastructure``, ``scoring_error``, ``sandbox_oom``. That is a
reissue-policy class, not a diagnosis, and for a whole family of failures it is
all a miner ever saw. Agent ``5fdadd33`` burned four validator leases in 82-108
seconds each behind a bare ``scoring_error``; the harness's own output named the
cause the entire time and lived only on validator hosts.

This command prints that output. Authentication is a signature from the hotkey
that owns the agent: no token to request, nothing to be granted, and no operator
in the loop. A miner reads their own rows and nobody else's.

Output formats:

- Default: per-validator human summary, with the log tail last
- ``--json``: full JSON response body (raw API shape, scriptable)

Exit codes:
- 0 success (agent found, attempts printed)
- 1 generic error (network, malformed UUID, no wallet resolvable)
- 3 not found (404 — unknown agent, not yours, or signature rejected)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from uuid import UUID

from ditto.api_models.miner_logs import (
    MinerHarnessLogAttempt,
    MinerHarnessLogsRequest,
)
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.errors import (
    AgentNotFoundError,
    ApiResponseError,
    WalletNotFoundError,
)
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.signing import sign_harness_logs_request
from ditto.miner_cli.wallet import load_wallet

logger = logging.getLogger(__name__)


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    """Register the ``logs`` subparser on the top-level argparse layout."""
    parser = subparsers.add_parser(
        "logs",
        help="Read your own agent's harness diagnostics (signed).",
        description=(
            "Print what each validator reported for one of your agents, "
            "including the failing harness's own output. Requires the "
            "wallet hotkey that owns the agent: the request is signed and "
            "the platform returns only agents that hotkey owns."
        ),
        parents=parents or [],
    )
    parser.add_argument(
        "agent_id",
        type=UUID,
        help="UUID of your agent to read diagnostics for.",
    )
    parser.add_argument(
        "--wallet.name",
        "--coldkey",
        dest="coldkey_name",
        default=os.environ.get("WALLET_NAME"),
        help=(
            "Coldkey wallet name. Aliases: --wallet.name / --coldkey. Env: WALLET_NAME."
        ),
    )
    parser.add_argument(
        "--wallet.hotkey",
        "--hotkey",
        dest="hotkey_name",
        default=os.environ.get("HOTKEY_NAME"),
        help="Hotkey name. Aliases: --wallet.hotkey / --hotkey. Env: HOTKEY_NAME.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the raw JSON response body instead of the human summary.",
    )
    parser.set_defaults(func=run)
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute the logs subcommand and return an exit code."""
    network = resolve_network(args.network)
    try:
        handle, live_wallet = load_wallet(
            coldkey_name=args.coldkey_name, hotkey_name=args.hotkey_name
        )
        # Serialized once, here, and reused verbatim in both the signature and
        # the body. Formatting it twice would risk two spellings of the same
        # instant, and the signature would not verify.
        requested_at = datetime.now(UTC)
        requested_at_wire = requested_at.isoformat(timespec="microseconds")
        body = MinerHarnessLogsRequest(
            miner_hotkey=handle.hotkey_ss58,
            agent_id=args.agent_id,
            requested_at=requested_at,
            signature=sign_harness_logs_request(
                handle=handle,
                live_wallet=live_wallet,
                agent_id=str(args.agent_id),
                requested_at=requested_at_wire,
            ),
        )
        with ApiClient(base_url=network.api_url) as client:
            response = client.post_harness_logs(body)
    except AgentNotFoundError as e:
        print(f"not found: {e}", file=sys.stderr)
        return 3
    except WalletNotFoundError as e:
        print(f"wallet error: {e}", file=sys.stderr)
        return 1
    except ApiResponseError as e:
        print(f"api error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(response.model_dump(mode="json"), indent=2))
        return 0

    print(f"agent:  {response.agent_id}")
    print(f"hotkey: {response.miner_hotkey}")
    print(f"status: {response.agent_status}")
    if not response.attempts:
        print("\nno validator attempts recorded yet.")
        return 0
    for attempt in response.attempts:
        _print_attempt(attempt)
    return 0


def _print_attempt(attempt: MinerHarnessLogAttempt) -> None:
    """Print one validator's attempt, log tail last and clearly delimited."""
    print(f"\n--- validator {attempt.validator_hotkey} (v{attempt.bench_version})")
    print(f"    status:   {attempt.status}")
    print(f"    issued:   {attempt.issued_at.isoformat()}")
    if attempt.failed_at is not None:
        # The single most diagnostic number for an early death: a lease is 90
        # minutes, so a lifetime in seconds says the harness never really ran.
        lifetime = (attempt.failed_at - attempt.issued_at).total_seconds()
        print(f"    failed:   {attempt.failed_at.isoformat()} ({lifetime:.1f}s in)")
    if attempt.failure_reason is not None:
        print(f"    reason:   {attempt.failure_reason}")
    if attempt.failure_detail is not None:
        print(f"    detail:   {attempt.failure_detail}")
    if attempt.container_log_tail is None:
        return
    # Fenced, and never interpreted. This is the harness's own output replayed
    # verbatim: it can contain ANSI escapes, partial lines, or anything else it
    # chose to print, so it gets an unambiguous boundary and no formatting.
    print("    harness output (last bytes before exit):")
    print("    " + "-" * 66)
    for line in attempt.container_log_tail.splitlines():
        print(f"    | {line}")
    print("    " + "-" * 66)
