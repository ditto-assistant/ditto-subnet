"""CLI: ``python -m query_adapter_pilot.miner``.

Binds loopback only, writes ``--ready-file`` atomically once listening, and
exits 0 on SIGTERM/SIGINT. Invalid or non-loopback settings exit 2.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from types import FrameType
from typing import Any

from ..contract import CONTRACT_VERSION
from .server import MinerConfig, start_miner
from .state import FAULT_MODES


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m query_adapter_pilot.miner",
        description="Synthetic-only, loopback-only query-adapter/v1 reference miner.",
    )
    p.add_argument("--host", default="127.0.0.1", help="loopback host: 127.0.0.1, ::1, localhost")
    p.add_argument("--port", type=int, default=0, help="0 picks a free port")
    p.add_argument("--token-file", required=True, type=Path, help="file holding the pilot token")
    p.add_argument("--cache-capacity", type=int, default=64)
    p.add_argument("--max-tenants", type=int, default=1024)
    p.add_argument("--fault", choices=FAULT_MODES, default="none", help="test-only fault mode")
    p.add_argument("--slow-ms", type=int, default=0)
    p.add_argument("--fault-seed", type=int, default=0)
    p.add_argument("--ready-file", type=Path, default=None, help="JSON written once listening")
    return p


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        token = args.token_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        print(f"miner: cannot read token file: {exc}", file=sys.stderr)
        return 2
    cfg = MinerConfig(
        host=args.host,
        port=args.port,
        token=token,
        cache_capacity=args.cache_capacity,
        max_tenants=args.max_tenants,
        fault=args.fault,
        slow_ms=args.slow_ms,
        fault_seed=args.fault_seed,
    )
    try:
        cfg.validate()
    except ValueError as exc:
        print(f"miner: {exc}", file=sys.stderr)
        return 2

    stop = threading.Event()

    def _on_signal(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        miner = start_miner(cfg)
    except (OSError, ValueError) as exc:
        print(f"miner: cannot start: {exc}", file=sys.stderr)
        return 2
    try:
        if args.ready_file is not None:
            _write_atomic(
                args.ready_file,
                {"contract": CONTRACT_VERSION, "url": miner.url, "pid": os.getpid()},
            )
        print(f"miner: listening on {miner.url} (fault={cfg.fault})", file=sys.stderr, flush=True)
        while not stop.wait(0.2):
            pass
    finally:
        miner.close()
        if args.ready_file is not None:
            args.ready_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
