"""``ditto-edge``: run a node on a Mac, or dry-run the lane against prod-shaped traffic.

    ditto-edge keygen --key ~/.ditto-edge/device.key
    ditto-edge node run --key ~/.ditto-edge/device.key --store ~/.ditto-edge/objects
    ditto-edge dry-run --synthetic 40 --share-id <public share id> --out report.json

A node only reaches Platform over outbound HTTPS. It holds public objects
Ditto assigned to it and nothing else. While the lane is shadow, nothing pays.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import math
import os
import platform
import signal
import sys
import threading
from pathlib import Path

from ditto_edge_protocol import __version__
from ditto_edge_protocol.dryrun import ADVERSARIES, DryRunConfig, run_dry_run
from ditto_edge_protocol.limits import GIB
from ditto_edge_protocol.node import EdgeNode, NodeConfig
from ditto_edge_protocol.signing import DeviceKey
from ditto_edge_protocol.sources import (
    DEFAULT_SHARE_BASE,
    PublicShareSource,
    SourceObject,
    synthetic_objects,
)
from ditto_edge_protocol.store import DirectoryStore
from ditto_edge_protocol.transport import HttpTransport
from ditto_edge_protocol.wire import ClientInfo, NodePlatform

DEFAULT_API = "https://platform-api.heyditto.ai"
DEFAULT_HOME = Path.home() / ".ditto-edge"


def _machine() -> NodePlatform:
    memory_gb = None
    with contextlib.suppress(ValueError, OSError, AttributeError):
        memory_gb = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // GIB)
    return NodePlatform(
        os=platform.system()[:32],
        arch=platform.machine()[:16],
        memory_gb=memory_gb,
    )


def _existing_key(args: argparse.Namespace) -> DeviceKey | None:
    """The key of an enrolled node. A new key would be a different node."""
    if not args.key.is_file():
        print(
            f"device key not found: {args.key}; create it with `ditto-edge keygen`",
            file=sys.stderr,
        )
        return None
    return DeviceKey.load(args.key)


def _node(args: argparse.Namespace, key: DeviceKey) -> EdgeNode:
    transport = HttpTransport(api_url=args.api_url, key=key)
    config = NodeConfig(
        public_key_hex=key.public_key_hex,
        quota_bytes=int(args.quota_gb * GIB),
        client=ClientInfo(name="ditto-edge-ref", version=__version__),
        platform=_machine(),
    )
    return EdgeNode(
        transport=transport, store=DirectoryStore(args.store), config=config
    )


def _cmd_keygen(args: argparse.Namespace) -> int:
    key = DeviceKey.load_or_create(args.key)
    print(key.node_id)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    node = _node(args, DeviceKey.load_or_create(args.key))
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    enrolled = node.enroll()
    print(
        f"enrolled {enrolled.node_id} (lane {enrolled.limits.mode}); polling",
        flush=True,
    )
    node.run(stop)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    key = _existing_key(args)
    if key is None:
        return 2
    print(_node(args, key).transport.status().model_dump_json(indent=2))
    return 0


def _cmd_leave(args: argparse.Namespace) -> int:
    key = _existing_key(args)
    if key is None:
        return 2
    _node(args, key).leave(reason="operator left")
    print("left the lane and deleted the local cache")
    return 0


def _quota_gb(value: str) -> float:
    try:
        quota = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    if not math.isfinite(quota) or quota < 0:
        raise argparse.ArgumentTypeError(
            "the quota is a finite number of GB, 0 or more"
        )
    return quota


def _share_ids(args: argparse.Namespace) -> list[str]:
    ids = list(args.share_id or [])
    if args.share_ids_file is not None:
        for line in args.share_ids_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                ids.append(line)
    return ids


def _cmd_dry_run(args: argparse.Namespace) -> int:
    adversaries = tuple(
        name for name in (a.strip() for a in args.adversaries.split(",")) if name
    )
    unknown = sorted(set(adversaries) - set(ADVERSARIES))
    if unknown:
        print(
            f"unknown adversaries {unknown}; choose from {list(ADVERSARIES)}",
            file=sys.stderr,
        )
        return 2
    share_ids = _share_ids(args)
    if args.recheck_public and not share_ids:
        print("--recheck-public needs --share-id or --share-ids-file", file=sys.stderr)
        return 2
    objects: list[SourceObject] = synthetic_objects(args.synthetic, seed=args.seed)
    source = None
    if share_ids:
        source = PublicShareSource(share_base=args.share_base)
        for share_id, obj in source.fetch_many(share_ids):
            if obj is None:
                print(
                    f"skipped {share_id}: not public, empty, or too large",
                    file=sys.stderr,
                )
            else:
                objects.append(obj)
    config = DryRunConfig(
        honest_nodes=args.honest,
        part_time_nodes=args.part_time,
        adversaries=adversaries,
        windows=args.windows,
        revoke_fraction=args.revoke_fraction,
        seed=args.seed,
    )
    report = run_dry_run(
        config, objects, public_source=source if args.recheck_public else None
    )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.out is not None:
        args.out.write_text(rendered + "\n", encoding="utf-8")
    verdict = report["verdict"]
    print(json.dumps({"objects": report["objects"], "verdict": verdict}, indent=2))
    return 0 if verdict["pass"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ditto-edge", description=__doc__.splitlines()[0]
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="create (or show) this device's node id")
    keygen.add_argument("--key", type=Path, default=DEFAULT_HOME / "device.key")
    keygen.set_defaults(func=_cmd_keygen)

    node = sub.add_parser("node", help="run a node against Platform")
    node_sub = node.add_subparsers(dest="node_command", required=True)
    for name, func, text in (
        ("run", _cmd_run, "enroll and poll until stopped"),
        ("status", _cmd_status, "show this node's status"),
        ("leave", _cmd_leave, "leave the lane and delete the cache"),
    ):
        cmd = node_sub.add_parser(name, help=text)
        cmd.add_argument("--api-url", default=DEFAULT_API)
        cmd.add_argument("--key", type=Path, default=DEFAULT_HOME / "device.key")
        cmd.add_argument("--store", type=Path, default=DEFAULT_HOME / "objects")
        cmd.add_argument("--quota-gb", type=_quota_gb, default=2.0)
        cmd.set_defaults(func=func)

    dry = sub.add_parser("dry-run", help="simulate the lane in one process")
    dry.add_argument("--honest", type=int, default=5)
    dry.add_argument("--part-time", type=int, default=1)
    dry.add_argument("--adversaries", default=",".join(ADVERSARIES))
    dry.add_argument("--windows", type=int, default=4)
    dry.add_argument("--synthetic", type=int, default=40)
    dry.add_argument("--share-id", action="append", help="a public share id to mirror")
    dry.add_argument("--share-ids-file", type=Path)
    dry.add_argument("--share-base", default=DEFAULT_SHARE_BASE)
    dry.add_argument(
        "--recheck-public",
        action="store_true",
        help="recheck shares anonymously each window and revoke unpublished ones",
    )
    dry.add_argument("--revoke-fraction", type=float, default=0.2)
    dry.add_argument("--seed", type=int, default=0)
    dry.add_argument("--out", type=Path)
    dry.set_defaults(func=_cmd_dry_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
