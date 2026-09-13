"""``ditto link-ditto``: attach your Ditto account to this hotkey.

Signing in with Ditto happens in a browser on dittobench.ai's OpenID flow; the
CLI only starts the attempt under the miner session ``ditto login`` created
(that session is the hotkey proof), opens the consent URL, and waits for the
Platform to confirm the link. No key material, TAO, or Ditto credentials pass
through this command, and the Ditto user id is whatever the Platform verified
from Ditto's signed id_token — never something the CLI asserts.

Subcommands:

- ``link-ditto``          start a link and wait for the browser round trip
- ``link-ditto status``   show the current link for this hotkey
- ``link-ditto unlink``   revoke the link (the Ditto account keeps its own data)

Exit codes: 0 linked / shown / revoked; 1 not signed in, refused, or timed out.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
import webbrowser

from ditto.api_models.miner_ditto_link import (
    MinerDittoLinkStartRequest,
    MinerDittoLinkView,
)
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.errors import ApiResponseError, LoginRequiredError
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.preferences import load_miner_session

POLL_SECONDS = 2.0
DEFAULT_WAIT_SECONDS = 600


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "link-ditto",
        help="Link your Ditto account to this hotkey with Sign in with Ditto.",
        description=(
            "Start a Sign in with Ditto round trip for the hotkey of your saved "
            "miner session, open the consent page in a browser, and wait for the "
            "Platform to record the verified link. Nothing is signed and no TAO "
            "moves."
        ),
        parents=parents or [],
    )
    parser.set_defaults(func=run, link_command="link")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the sign-in URL instead of opening a browser.",
    )
    parser.add_argument(
        "--wait",
        dest="wait_seconds",
        type=int,
        default=DEFAULT_WAIT_SECONDS,
        help=(
            "Seconds to wait for the browser round trip "
            f"(default {DEFAULT_WAIT_SECONDS})."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print the result as JSON.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the pairing without asking once Ditto has signed you in.",
    )
    subs = parser.add_subparsers(dest="link_command")
    status = subs.add_parser(
        "status", help="Show the Ditto account linked to this hotkey."
    )
    status.set_defaults(func=run, link_command="status")
    status.add_argument("--json", action="store_true", help="Print the link as JSON.")
    unlink = subs.add_parser(
        "unlink", help="Revoke the Ditto account link for this hotkey."
    )
    unlink.set_defaults(func=run, link_command="unlink")
    return parser


def _session_token(args: argparse.Namespace) -> tuple[str, str] | None:
    network = resolve_network(args.network).name
    saved = load_miner_session(network=network)
    if saved is None:
        print(
            "not signed in: run `ditto login` first (the session proves the hotkey)",
            file=sys.stderr,
        )
        return None
    return str(saved["token"]), str(saved["hotkey"])


def _print_link(link: MinerDittoLinkView | None, *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                link.model_dump(mode="json") if link else None, indent=2, sort_keys=True
            )
        )
        return
    if link is None:
        print("no Ditto account is linked to this hotkey")
        return
    who = link.ditto_email or link.ditto_user_id
    print(
        f"{link.miner_hotkey} is linked to Ditto account {who} (via {link.linked_via})"
    )


def _confirm_pairing(*, hotkey: str, who: str, skip: bool) -> bool:
    """Ask before writing the link. ``--yes`` skips the question; a non-TTY
    without ``--yes`` refuses, because silence must never mean consent."""
    print(f"Ditto signed in as {who}.")
    if skip:
        return True
    if not sys.stdin.isatty():
        print(
            "refusing to link without confirmation on a non-interactive terminal; "
            "re-run with --yes if this is the account you just signed in with",
            file=sys.stderr,
        )
        return False
    try:
        answer = input(f"Link hotkey {hotkey} to {who}? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def run(args: argparse.Namespace) -> int:
    command = getattr(args, "link_command", "link") or "link"
    found = _session_token(args)
    if found is None:
        return 1
    token, hotkey = found
    network = resolve_network(args.network)
    as_json = bool(getattr(args, "json", False))
    try:
        with ApiClient(base_url=network.api_url) as client:
            if command == "status":
                current = client.get_ditto_link(token=token)
                _print_link(current.link, as_json=as_json)
                if not current.enabled and current.link is None:
                    print(
                        "Ditto account linking is not enabled on this deployment",
                        file=sys.stderr,
                    )
                return 0
            if command == "unlink":
                client.delete_ditto_link(token=token)
                print(f"unlinked the Ditto account from {hotkey}")
                return 0
            started = client.start_ditto_link(
                token=token, body=MinerDittoLinkStartRequest(client="cli")
            )
            print(f"Sign in with Ditto to link {hotkey}:")
            print(started.authorize_url)
            if not args.no_browser:
                # A headless box has no browser; the printed URL is the fallback.
                with contextlib.suppress(Exception):
                    webbrowser.open(started.authorize_url, new=2)
            deadline = time.monotonic() + max(
                5, min(args.wait_seconds, started.expires_in)
            )
            while time.monotonic() < deadline:
                attempt = client.get_ditto_link_attempt(
                    token=token, attempt_id=started.attempt_id
                )
                if attempt.status == "authenticated":
                    # Ditto verified who signed in; nothing is linked until this
                    # hotkey's owner says so. A sign-in link someone else sent
                    # can therefore never attach their hotkey to your account.
                    who = (
                        attempt.ditto_email
                        or attempt.ditto_user_id
                        or "unknown account"
                    )
                    if not _confirm_pairing(hotkey=hotkey, who=who, skip=args.yes):
                        print(
                            "not linked (the sign-in expires on its own)",
                            file=sys.stderr,
                        )
                        return 1
                    attempt = client.confirm_ditto_link(
                        token=token, attempt_id=started.attempt_id
                    )
                if attempt.status == "linked":
                    _print_link(attempt.link, as_json=as_json)
                    return 0
                if attempt.status in ("failed", "expired"):
                    print(
                        f"link {attempt.status}: {attempt.error or 'no detail'}",
                        file=sys.stderr,
                    )
                    return 1
                time.sleep(POLL_SECONDS)
            print("timed out waiting for the browser sign-in", file=sys.stderr)
            return 1
    except LoginRequiredError as exc:
        print(f"{exc} — run `ditto login` again", file=sys.stderr)
        return 1
    except ApiResponseError as exc:
        print(str(exc), file=sys.stderr)
        return 1
