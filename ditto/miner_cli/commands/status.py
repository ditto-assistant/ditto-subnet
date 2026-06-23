"""``ditto status``: poll lifecycle by agent id or wallet hotkey.

Two resolution modes:

- Positional ``agent_id`` given → call
  ``/retrieval/agent/{agent_id}/status`` directly.
- No positional → fall back to the wallet hotkey (``--wallet.hotkey`` /
  ``--hotkey`` or ``HOTKEY_NAME`` env) and call
  ``/retrieval/agent-by-hotkey`` to resolve the latest agent for that
  hotkey.

Output formats:

- Default: plain-text human summary
- ``--json``: full JSON response body (raw API shape, scriptable)

Exit codes:
- 0 success (agent found, status printed)
- 1 generic error (network, malformed UUID, no hotkey resolvable)
- 3 not found (404 from API)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from uuid import UUID

from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.errors import (
    AgentNotFoundError,
    ApiResponseError,
    HotkeyAgentNotFoundError,
    WalletNotFoundError,
)
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.wallet import load_wallet

logger = logging.getLogger(__name__)


def add_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``status`` subparser on the top-level argparse layout."""
    parser = subparsers.add_parser(
        "status",
        help="Poll agent lifecycle status by id or wallet hotkey.",
        description=(
            "Look up an agent's current status. If agent_id is omitted, "
            "the wallet hotkey is resolved (via --wallet.hotkey / "
            "--hotkey / HOTKEY_NAME env) and the latest agent for that "
            "hotkey is returned."
        ),
    )
    parser.add_argument(
        "agent_id",
        nargs="?",
        type=UUID,
        default=None,
        help="UUID of the agent to look up. If omitted, falls back to hotkey.",
    )
    parser.add_argument(
        "--wallet.name",
        "--coldkey",
        dest="coldkey_name",
        default=os.environ.get("WALLET_NAME"),
        help=(
            "Coldkey wallet name (only needed for hotkey-resolution "
            "fallback path). Flag aliases: --wallet.name / --coldkey. "
            "Env: WALLET_NAME."
        ),
    )
    parser.add_argument(
        "--wallet.hotkey",
        "--hotkey",
        dest="hotkey_name",
        default=os.environ.get("HOTKEY_NAME"),
        help=(
            "Hotkey name (only needed for hotkey-resolution fallback "
            "path). Flag aliases: --wallet.hotkey / --hotkey. Env: "
            "HOTKEY_NAME."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the raw JSON response body instead of the human summary.",
    )
    parser.set_defaults(func=run)
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute the status subcommand and return an exit code."""
    network = resolve_network(args.network)
    try:
        with ApiClient(base_url=network.api_url) as client:
            if args.agent_id is not None:
                return _status_by_id(client, agent_id=args.agent_id, as_json=args.json)
            return _status_by_hotkey(
                client,
                coldkey_name=args.coldkey_name,
                hotkey_name=args.hotkey_name,
                as_json=args.json,
            )
    except (AgentNotFoundError, HotkeyAgentNotFoundError) as e:
        print(f"not found: {e}", file=sys.stderr)
        return 3
    except WalletNotFoundError as e:
        print(f"wallet error: {e}", file=sys.stderr)
        return 1
    except ApiResponseError as e:
        print(f"api error: {e}", file=sys.stderr)
        return 1


def _status_by_id(client: ApiClient, *, agent_id: UUID, as_json: bool) -> int:
    response = client.get_agent_status(agent_id=agent_id)
    if as_json:
        print(
            json.dumps(
                {"agent_id": str(response.agent_id), "status": response.status.value}
            )
        )
    else:
        print(f"Agent:  {response.agent_id}")
        print(f"Status: {response.status.value}")
    return 0


def _status_by_hotkey(
    client: ApiClient,
    *,
    coldkey_name: str | None,
    hotkey_name: str | None,
    as_json: bool,
) -> int:
    if not coldkey_name or not hotkey_name:
        print(
            "error: no agent_id supplied and wallet hotkey unresolved. "
            "Pass --wallet.name and --wallet.hotkey (or --coldkey / "
            "--hotkey) or set WALLET_NAME / HOTKEY_NAME.",
            file=sys.stderr,
        )
        return 1
    handle, _live = load_wallet(coldkey_name=coldkey_name, hotkey_name=hotkey_name)
    agent = client.get_agent_by_hotkey(miner_hotkey=handle.hotkey_ss58)
    if as_json:
        print(
            json.dumps(
                {
                    "agent_id": str(agent.agent_id),
                    "miner_hotkey": agent.miner_hotkey,
                    "name": agent.name,
                    "status": agent.status.value,
                    "sha256": agent.sha256,
                    "created_at": agent.created_at.isoformat(),
                }
            )
        )
    else:
        print(f"Agent:   {agent.agent_id}")
        print(f"Hotkey:  {agent.miner_hotkey}")
        print(f"Name:    {agent.name}")
        print(f"Status:  {agent.status.value}")
        print(f"sha256:  {agent.sha256}")
        print(f"Created: {agent.created_at.isoformat()}")
    return 0
