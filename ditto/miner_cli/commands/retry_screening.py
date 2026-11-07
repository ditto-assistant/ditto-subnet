"""``ditto retry-screening``: retry your own interrupted screening, self-serve.

A screening attempt that fails for an infrastructure-shaped reason parks the
submission in ``screening_failed`` with "manual retry required". This command
authorizes that retry without an operator DM (SN-456): it reads the agent's
retry status, shows what the retry spends, asks for confirmation, and then
authorizes exactly the attempt it showed.

Rules Platform enforces (the command only reports them):

- the miner session must hold the ``upload`` scope
  (``ditto login --scopes read,upload``);
- only your own agent, only after an interrupted attempt -- never a
  rejection, quarantine, operator hold, or a failure your submission owns;
- a small per-agent budget, and operators can pause the route;
- re-running is safe: a retry already queued for that attempt is reported,
  not duplicated.

Exit codes:
- 0 retry authorized, already queued, or ``--check`` printed
- 1 generic error (network, not signed in, cancelled)
- 3 not found (unknown agent or not yours)
- 4 not eligible right now (the reason is printed)
"""

from __future__ import annotations

import argparse
import json
import sys
from uuid import UUID

from ditto.api_models.miner_screening_retry import (
    MinerScreeningRetryRequest,
    MinerScreeningRetryStatus,
)
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.confirm import confirm_screening_retry
from ditto.miner_cli.errors import (
    AgentNotFoundError,
    ApiResponseError,
    LoginRequiredError,
    ScreeningRetryCancelledError,
    ScreeningRetryRejectedError,
)
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.preferences import load_miner_session

EXIT_NOT_ELIGIBLE = 4

_BLOCKER_HELP: dict[str, str] = {
    "not_screening_failed": (
        "the submission is not parked after a failed screening; "
        "`ditto status` shows where it is"
    ),
    "no_attempt": "no screening attempt has run yet; wait for the screener",
    "attempt_active": "a screening attempt is still running; wait for it",
    "attempt_not_retryable": (
        "the latest screening ended in a review ruling; appeal from the "
        "submission page instead of retrying"
    ),
    "reason_not_retryable": (
        "the latest failure is owned by the submission (fix it and upload "
        "a new version) or is an operator hold"
    ),
    "disabled": (
        "operators have paused self-serve retries, usually during an "
        "incident; your submission is not lost, try again later"
    ),
    "budget_exhausted": (
        "this agent has used its self-serve retries; ask an operator, "
        "quoting the agent and attempt ids"
    ),
}


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "retry-screening",
        help="Retry your own interrupted screening (signed-in session).",
        description=(
            "Authorize one retry of your agent's interrupted screening "
            "('manual retry required'). Requires a miner session with the "
            "upload scope: `ditto login --scopes read,upload`."
        ),
        parents=parents or [],
    )
    parser.add_argument("agent_id", type=UUID, help="UUID of your agent.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only print whether a retry is available; change nothing.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (scripts).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit raw JSON (status with --check, else the retry result).",
    )
    parser.set_defaults(func=run)
    return parser


def _print_status(status: MinerScreeningRetryStatus) -> None:
    print(f"agent:   {status.agent_id}")
    print(f"status:  {status.agent_status}")
    if status.latest_attempt_id is not None:
        print(f"attempt: {status.latest_attempt_id} ({status.latest_attempt_status})")
    reason = " ".join(
        part for part in (status.reason_code, status.public_reason) if part
    )
    if reason:
        print(f"reason:  {reason}")
    print(
        f"budget:  {status.retries_used} of {status.retries_allowed} "
        "self-serve retries used"
    )


def _explain_blocker(blocker: str | None) -> str:
    if blocker is None:
        return "not eligible"
    return f"{blocker}: {_BLOCKER_HELP.get(blocker, 'not eligible right now')}"


def run(args: argparse.Namespace) -> int:
    network = resolve_network(args.network)
    saved = load_miner_session(network=network.name)
    if saved is None:
        print(
            "not signed in. run `ditto login --scopes read,upload` and retry.",
            file=sys.stderr,
        )
        return 1
    token = str(saved["token"])
    try:
        with ApiClient(base_url=network.api_url) as client:
            status = client.get_screening_retry_status(
                agent_id=args.agent_id, token=token
            )
            if args.check:
                if args.json:
                    print(json.dumps(status.model_dump(mode="json"), indent=2))
                    return 0
                _print_status(status)
                if status.already_authorized:
                    print("\na retry is already queued for this attempt.")
                elif status.eligible:
                    print("\na retry is available: run without --check.")
                else:
                    print(f"\nretry unavailable: {_explain_blocker(status.blocker)}")
                return 0
            if not args.json:
                _print_status(status)
            if status.already_authorized:
                # Nothing to spend: report the queued grant and change nothing.
                if args.json:
                    print(json.dumps(status.model_dump(mode="json"), indent=2))
                else:
                    print("\na retry is already queued for this attempt.")
                return 0
            if not status.eligible:
                print(
                    f"retry unavailable: {_explain_blocker(status.blocker)}",
                    file=sys.stderr,
                )
                return EXIT_NOT_ELIGIBLE
            assert status.latest_attempt_id is not None
            confirm_screening_retry(
                agent_id=str(status.agent_id),
                attempt_id=str(status.latest_attempt_id),
                reason=status.reason_code or status.public_reason or "unknown",
                retries_used=status.retries_used,
                retries_allowed=status.retries_allowed,
                cost_notice=status.cost_notice,
                skip=bool(args.yes),
            )
            result = client.retry_screening(
                agent_id=args.agent_id,
                token=token,
                body=MinerScreeningRetryRequest(
                    expected_attempt_id=status.latest_attempt_id,
                    expected_sha256=status.artifact_sha256,
                    acknowledge_cost=True,
                ),
            )
    except LoginRequiredError as e:
        print(f"session expired: {e}", file=sys.stderr)
        return 1
    except AgentNotFoundError as e:
        print(f"not found: {e}", file=sys.stderr)
        return 3
    except ScreeningRetryCancelledError as e:
        print(str(e), file=sys.stderr)
        return 1
    except ScreeningRetryRejectedError as e:
        message = str(e)
        if "missing the upload scope" in message:
            message += (
                "\nsign in with the upload scope: `ditto login --scopes read,upload`"
            )
        print(f"retry refused: {message}", file=sys.stderr)
        return EXIT_NOT_ELIGIBLE
    except ApiResponseError as e:
        print(f"api error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.model_dump(mode="json"), indent=2))
        return 0
    if result.idempotent:
        who = "an operator" if result.authorized_by_operator else "you"
        print(f"\na retry for this attempt was already queued by {who}.")
    else:
        print(
            f"\nretry authorized ({result.retries_used} of "
            f"{result.retries_allowed} used). a screener will pick it up; "
            f"follow it with `ditto status {result.agent_id}`."
        )
    print(f"grant:   {result.override_id}")
    return 0
