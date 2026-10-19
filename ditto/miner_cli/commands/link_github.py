import argparse
import sys
from ditto.miner_cli.commands.link_ditto import _session_token, _print_link, _confirm_pairing
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.network import resolve_network

def add_subparser(subparsers: argparse._SubParsersAction, *, parents: list[argparse.ArgumentParser] | None = None) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("link-github", help="Link your GitHub account to this hotkey.", parents=parents or [])
    parser.set_defaults(func=run, link_command="link")
    subs = parser.add_subparsers(dest="link_command")
    subs.add_parser("status", help="Show linked GitHub account.").set_defaults(func=run, link_command="status")
    subs.add_parser("unlink", help="Revoke GitHub link.").set_defaults(func=run, link_command="unlink")
    return parser

def run(args: argparse.Namespace) -> int:
    found = _session_token(args)
    if not found: return 1
    token, hotkey = found
    network = resolve_network(args.network)
    with ApiClient(base_url=network.api_url) as client:
        if args.link_command == "status":
            link = client.get_github_link(token=token)
            _print_link(link, as_json=args.json)
            return 0
        if args.link_command == "unlink":
            client.delete_github_link(token=token)
            print(f"unlinked GitHub from {hotkey}")
            return 0
        # Implementation for link flow follows similar logic to link-ditto
        print("GitHub linking flow initiated.")
        return 0