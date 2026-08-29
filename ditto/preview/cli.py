"""CLI: ``uv run python -m ditto.preview …`` / ``ditto-preview``."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sys

from ditto.preview.client import PreviewClient
from ditto.preview.composition import CompositionError, compose
from ditto.preview.engine import IsolationError, PreviewEngine
from ditto.preview.identity import preview_id
from ditto.preview.orchestrator import load_latest_state, plan_as_dict, up
from ditto.preview.server import serve_forever
from ditto.preview.urls import plan_urls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ditto-preview",
        description="Isolated SN118 preview channels and Foundry-style cheatcodes.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    compose_p = sub.add_parser("compose", help="Resolve a multi-select profile set")
    compose_p.add_argument(
        "profiles", help="comma-separated: dashboard,backroom,stack,stack-copy"
    )
    compose_p.add_argument("--attach-prod-api", action="store_true")
    compose_p.add_argument("--ref", default="local")
    compose_p.add_argument("--sha", default="0" * 40)

    serve_p = sub.add_parser("serve", help="Run preview-control HTTP")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument(
        "--port", type=int, default=int(os.environ.get("PREVIEW_PORT", "4077"))
    )
    serve_p.add_argument(
        "--network", default=os.environ.get("PREVIEW_NETWORK", "local")
    )
    serve_p.add_argument(
        "--endpoint",
        default=os.environ.get("PREVIEW_ENDPOINT", "ws://127.0.0.1:9944"),
    )
    serve_p.add_argument(
        "--netuid", type=int, default=int(os.environ.get("PREVIEW_NETUID", "3"))
    )
    serve_p.add_argument(
        "--validator-hotkey",
        default=os.environ.get("PREVIEW_VALIDATOR_HOTKEY", ""),
        help="optional localnet validator to register and permit at boot",
    )

    up_p = sub.add_parser("up", help="Start a local preview for the given profiles")
    up_p.add_argument("profiles")
    up_p.add_argument("--attach-prod-api", action="store_true")
    up_p.add_argument("--ref", default="local")
    up_p.add_argument("--sha", default="0" * 40)
    up_p.add_argument(
        "--postgres", action="store_true", help="docker compose up postgres"
    )
    up_p.add_argument(
        "--upstream",
        default=os.environ.get("PREVIEW_UPSTREAM_URL", "http://127.0.0.1:11434"),
        help="local relay upstream for the fault proxy",
    )

    sub.add_parser("down", help="Print how to stop the latest local preview")
    sub.add_parser("urls", help="Print the latest local preview URLs")
    sub.add_parser("state", help="Print overlay state from the latest control URL")

    ctl = sub.add_parser("ctl", help="Send a cheatcode to preview-control")
    ctl.add_argument("cheat")
    ctl.add_argument("--url", default=os.environ.get("PREVIEW_CONTROL_URL", ""))
    ctl.add_argument("--hotkey")
    ctl.add_argument("--permit", action="store_true")
    ctl.add_argument("--stake", type=float, default=0.0)
    ctl.add_argument("--n", type=int, default=1)
    ctl.add_argument("--lease-id")
    ctl.add_argument("--grant-id")
    ctl.add_argument("--status", type=int)
    ctl.add_argument("--name")
    ctl.add_argument("--json-path")
    ctl.add_argument("--database-url")
    ctl.add_argument("--dropped", action="store_true")
    ctl.add_argument("--clear", action="store_true")

    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except (
        CompositionError,
        IsolationError,
        ValueError,
        FileNotFoundError,
        RuntimeError,
    ) as exc:
        print(exc, file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    if args.cmd == "compose":
        plan = compose(args.profiles.split(","), attach_prod_api=args.attach_prod_api)
        identity = preview_id(args.ref, args.sha)
        payload = plan_as_dict(plan) | {
            "id": identity,
            "urls": plan_urls(
                plan, identity, control_url="http://127.0.0.1:4077", local=True
            ),
        }
        print(json.dumps(payload, indent=2, default=list))
        return 0
    if args.cmd == "serve":
        token = os.environ.get("PREVIEW_CONTROL_TOKEN") or secrets.token_urlsafe(32)
        engine = PreviewEngine(
            network=args.network,
            endpoint=args.endpoint,
            netuid=args.netuid,
        )
        if args.validator_hotkey:
            engine.register(args.validator_hotkey, permit=True, stake=1.0)
        print(
            f"preview-control on http://{args.host}:{args.port} network={args.network}",
            flush=True,
        )
        print(f"PREVIEW_CONTROL_TOKEN={token}", file=sys.stderr, flush=True)
        serve_forever(engine, host=args.host, port=args.port, token=token)
        return 0
    if args.cmd == "up":
        import threading

        handle = up(
            args.profiles.split(","),
            ref=args.ref,
            sha=args.sha,
            attach_prod_api=args.attach_prod_api,
            start_postgres=args.postgres,
            upstream=args.upstream,
        )
        print(
            json.dumps({"id": handle.identity, "urls": handle.urls}, indent=2),
            flush=True,
        )
        print("preview-control running; Ctrl-C to stop", file=sys.stderr, flush=True)
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(
            signal.SIGTERM,
            lambda _signum, _frame: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            handle.down()
            signal.signal(signal.SIGTERM, previous_sigterm)
        return 0
    if args.cmd == "down":
        raise RuntimeError(
            "preview up runs in the foreground; press Ctrl-C in its terminal "
            "so cleanup can be verified"
        )
    if args.cmd == "urls":
        print(json.dumps(load_latest_state().get("urls", {}), indent=2))
        return 0
    if args.cmd == "state":
        url, token = _control_config(args if hasattr(args, "url") else None)
        client = PreviewClient(url, token=token)
        print(json.dumps(client.state(), indent=2))
        return 0
    if args.cmd == "ctl":
        return _ctl(args)
    return 2


def _control_config(args: argparse.Namespace | None) -> tuple[str, str]:
    if args is not None and getattr(args, "url", None):
        url = str(args.url)
    else:
        env = os.environ.get("PREVIEW_CONTROL_URL", "").strip()
        url = env
    token = os.environ.get("PREVIEW_CONTROL_TOKEN", "").strip()
    state: dict[str, object] = {}
    try:
        state = load_latest_state()
    except FileNotFoundError:
        if not url:
            raise
    if not url:
        urls = state.get("urls") or {}
        if isinstance(urls, dict):
            url = str(urls.get("control") or "")
    if not token:
        state_urls = state.get("urls") or {}
        state_url = (
            str(state_urls.get("control") or "") if isinstance(state_urls, dict) else ""
        )
        if not state_url or state_url == url:
            token = str(state.get("control_token") or "")
    if not url:
        raise FileNotFoundError("set PREVIEW_CONTROL_URL or run ditto-preview up")
    if not token:
        raise FileNotFoundError(
            "set PREVIEW_CONTROL_TOKEN or use the worktree that started the preview"
        )
    return url, token


def _ctl(args: argparse.Namespace) -> int:
    url, token = _control_config(args)
    client = PreviewClient(url, token=token)
    cheat = args.cheat.replace("-", "_")
    if cheat == "register":
        if not args.hotkey:
            raise ValueError("--hotkey is required")
        print(
            json.dumps(
                client.register(args.hotkey, permit=args.permit, stake=args.stake)
            )
        )
        return 0
    if cheat == "permit":
        if not args.hotkey:
            raise ValueError("--hotkey is required")
        print(json.dumps(client.permit(args.hotkey, enabled=not args.clear)))
        return 0
    if cheat == "warp_block":
        print(json.dumps(client.warp_block(args.n)))
        return 0
    if cheat == "warp_tempo":
        print(json.dumps(client.warp_tempo(args.n)))
        return 0
    if cheat == "issue_lease":
        if not args.hotkey:
            raise ValueError("--hotkey is required")
        print(json.dumps(client.issue_lease(args.hotkey)))
        return 0
    if cheat == "expire_lease":
        print(json.dumps(client.expire_lease(args.lease_id)))
        return 0
    if cheat == "issue_grant":
        print(json.dumps(client.issue_grant()))
        return 0
    if cheat == "exhaust_allowance":
        print(json.dumps(client.exhaust_allowance(args.grant_id)))
        return 0
    if cheat == "inject_provider":
        status = None if args.clear else args.status
        print(json.dumps(client.inject_provider(status)))
        return 0
    if cheat == "drop_relay":
        print(json.dumps(client.drop_relay(dropped=not args.clear)))
        return 0
    if cheat == "snapshot":
        if not args.name:
            raise ValueError("--name is required")
        print(json.dumps(client.snapshot(args.name)))
        return 0
    if cheat == "revert":
        if not args.name:
            raise ValueError("--name is required")
        print(json.dumps(client.revert(args.name)))
        return 0
    if cheat == "align_from_db":
        print(
            json.dumps(
                client.align_from_db(
                    json_path=args.json_path,
                    database_url=args.database_url
                    or os.environ.get("PREVIEW_DATABASE_URL"),
                )
            )
        )
        return 0
    raise ValueError(f"unknown cheatcode {args.cheat}")


if __name__ == "__main__":
    raise SystemExit(main())
