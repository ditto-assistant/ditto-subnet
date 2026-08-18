"""``ditto logs``: read your own agent's harness diagnostics, self-serve.

When a submission fails scoring, the ticket ledger says only *how* the platform
responded -- ``infrastructure``, ``scoring_error``, ``sandbox_oom``. That is a
reissue-policy class, not a diagnosis, and for a whole family of failures it is
all a miner ever saw. Agent ``5fdadd33`` burned four validator leases in 82-108
seconds each behind a bare ``scoring_error``; the harness's own output named the
cause the entire time and lived only on validator hosts.

This command prints that output. Authentication is the miner session created by
``ditto login`` (or the dashboard / hosted MCP sign-in). A miner reads their
own rows and nobody else's.

Output formats:

- Default: per-validator human summary, with the log tail last. ANSI and
  other terminal control sequences in the tail are escaped so a visual
  fence cannot be used to hijack the terminal.
- ``--json``: full JSON response body (raw API shape, scriptable)

Exit codes:
- 0 success (agent found, attempts printed)
- 1 generic error (network, malformed UUID, not signed in)
- 3 not found (404 — unknown agent or not yours)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from uuid import UUID

from ditto.api_models.miner_logs import MinerHarnessLogAttempt
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.errors import (
    AgentNotFoundError,
    ApiResponseError,
    LoginRequiredError,
)
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.preferences import load_miner_session

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_harness_output(text: str) -> str:
    """Strip ANSI and escape remaining C0 controls for a terminal.

    A visual fence around the tail does not stop the terminal from
    interpreting CSI sequences or other control bytes the harness printed.
    Human output therefore never writes those bytes raw.
    """
    stripped = _ANSI_RE.sub("", text)
    return _CTRL_RE.sub(lambda match: f"\\x{ord(match.group()):02x}", stripped)


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    """Register the ``logs`` subparser on the top-level argparse layout."""
    parser = subparsers.add_parser(
        "logs",
        help="Read your own agent's harness diagnostics (signed-in session).",
        description=(
            "Print what each validator reported for one of your agents, "
            "including the failing harness's own output. Requires a miner "
            "session from `ditto login` (or the dashboard / MCP sign-in)."
        ),
        parents=parents or [],
    )
    parser.add_argument(
        "agent_id",
        type=UUID,
        help="UUID of your agent to read diagnostics for.",
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
    saved = load_miner_session(network=network.name)
    if saved is None:
        print(
            "not signed in. run `ditto login` (or approve the code on "
            "dittobench.ai/#/reviews) and retry.",
            file=sys.stderr,
        )
        return 1
    try:
        with ApiClient(base_url=network.api_url) as client:
            response = client.get_harness_logs(
                agent_id=args.agent_id, token=str(saved["token"])
            )
    except LoginRequiredError as e:
        print(f"session expired: {e}", file=sys.stderr)
        return 1
    except AgentNotFoundError as e:
        print(f"not found: {e}", file=sys.stderr)
        return 3
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
    """Print one validator ticket, log tail last and clearly delimited."""
    print(
        f"\n--- validator {attempt.validator_hotkey} "
        f"(v{attempt.bench_version}, attempt {attempt.attempt_count})"
    )
    print(f"    status:   {attempt.status}")
    print(f"    issued:   {attempt.issued_at.isoformat()}")
    if attempt.stale:
        tail_from = (
            str(attempt.log_tail_attempt)
            if attempt.log_tail_attempt is not None
            else "a prior lease"
        )
        print(
            f"    stale:    evidence is from {tail_from}; "
            f"current lease is attempt {attempt.attempt_count}"
        )
    if attempt.failed_at is not None:
        # Only compute a lifetime when both timestamps belong to this lease.
        # After reissue, issued_at is new and failed_at is old.
        if not attempt.stale and attempt.failed_at >= attempt.issued_at:
            lifetime = (attempt.failed_at - attempt.issued_at).total_seconds()
            print(f"    failed:   {attempt.failed_at.isoformat()} ({lifetime:.1f}s in)")
        else:
            print(f"    failed:   {attempt.failed_at.isoformat()}")
    if attempt.failure_reason is not None:
        print(f"    reason:   {attempt.failure_reason}")
    if attempt.failure_detail is not None:
        print(f"    detail:   {attempt.failure_detail}")
    if attempt.container_log_tail is None:
        return
    print("    harness output (last bytes before exit):")
    print("    " + "-" * 66)
    for line in sanitize_harness_output(attempt.container_log_tail).splitlines():
        print(f"    | {line}")
    print("    " + "-" * 66)
