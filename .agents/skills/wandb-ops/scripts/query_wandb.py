#!/usr/bin/env python3
"""Read bounded operational evidence from the W&B public API.

The helper deliberately has no config or environment dump command. Authentication
is delegated to the W&B client, which reads WANDB_API_KEY without this script ever
printing or otherwise handling its value.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_ENTITY = "heyditto"
DEFAULT_PROJECT = "ditto-sn118"
DEFAULT_SUMMARY_PREFIXES = ("weights/", "ledger/", "sweep/")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _emit(value: Any) -> None:
    json.dump(_json_safe(value), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _wandb() -> Any:
    try:
        import wandb  # type: ignore[import-not-found]
    except ImportError as error:
        raise SystemExit(
            "wandb is not installed; run with `uv run --with wandb python ...`"
        ) from error
    return wandb


def _api(timeout: int) -> Any:
    if not os.environ.get("WANDB_API_KEY"):
        print(
            "warning: WANDB_API_KEY is not exported; the W&B client may use an "
            "existing authenticated profile",
            file=sys.stderr,
        )
    return _wandb().Api(timeout=timeout)


def _run_path(args: argparse.Namespace, run_id: str) -> str:
    if run_id.count("/") == 2:
        return run_id
    return f"{args.entity}/{args.project}/{run_id}"


def _parse_created_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _run_record(run: Any) -> dict[str, Any]:
    return {
        "id": run.id,
        "name": run.name,
        "state": run.state,
        "created_at": run.created_at,
        "updated_at": getattr(run, "updated_at", None),
        "url": run.url,
    }


def _cmd_runs(args: argparse.Namespace) -> None:
    filters: dict[str, Any] = {}
    if args.state:
        filters["state"] = {"$in": args.state}
    cutoff = (
        datetime.now(UTC) - timedelta(hours=args.since_hours)
        if args.since_hours is not None
        else None
    )
    if cutoff is not None:
        filters["created_at"] = {"$gte": cutoff.isoformat()}

    runs = _api(args.timeout).runs(
        f"{args.entity}/{args.project}",
        filters=filters or None,
        order="-created_at",
        per_page=min(max(args.limit, 1), 100),
    )
    records = []
    for run in runs:
        created_at = _parse_created_at(run.created_at)
        if cutoff is not None and created_at is not None and created_at < cutoff:
            break
        if args.name_contains and args.name_contains not in (run.name or ""):
            continue
        records.append(_run_record(run))
        if len(records) >= args.limit:
            break
    _emit(records)


def _selected_summary(
    summary: Mapping[str, Any], *, keys: Iterable[str], prefixes: Iterable[str]
) -> dict[str, Any]:
    requested_keys = set(keys)
    requested_prefixes = tuple(prefixes)
    return {
        key: value
        for key, value in summary.items()
        if key in requested_keys or key.startswith(requested_prefixes)
    }


def _cmd_summary(args: argparse.Namespace) -> None:
    api = _api(args.timeout)
    prefixes = args.prefix or list(DEFAULT_SUMMARY_PREFIXES)
    records = []
    for run_id in args.run:
        run = api.run(_run_path(args, run_id))
        records.append(
            {
                "run": _run_record(run),
                "summary": _selected_summary(
                    dict(run.summary), keys=args.key, prefixes=prefixes
                ),
            }
        )
    _emit(records)


def _cmd_history(args: argparse.Namespace) -> None:
    if not args.key:
        raise SystemExit("history requires at least one --key")
    run = _api(args.timeout).run(_run_path(args, args.run))
    last_step = getattr(run, "lastHistoryStep", None)
    min_step = None
    if isinstance(last_step, int) and args.lookback_steps is not None:
        min_step = max(0, last_step - args.lookback_steps)
    rows = run.scan_history(
        keys=["_timestamp", *args.key],
        page_size=min(args.page_size, 10_000),
        min_step=min_step,
    )
    tail: list[dict[str, Any]] = []
    for row in rows:
        if not any(key in row for key in args.key):
            continue
        tail.append(dict(row))
        if len(tail) > args.tail:
            tail.pop(0)
    _emit({"run": _run_record(run), "history": tail})


def _cmd_table(args: argparse.Namespace) -> None:
    run = _api(args.timeout).run(_run_path(args, args.run))
    pointer = dict(run.summary).get(args.name)
    try:
        pointer_data = dict(pointer)
    except (TypeError, ValueError):
        pointer_data = {}
    if not isinstance(pointer_data.get("path"), str):
        raise SystemExit(f"summary key {args.name!r} is not a W&B table pointer")
    remote_path = str(pointer_data["path"])
    with tempfile.TemporaryDirectory(prefix="wandb-query-") as directory:
        downloaded = run.file(remote_path).download(root=directory, replace=True)
        data = json.loads(Path(downloaded.name).read_text())
    columns = data.get("columns", [])
    rows = data.get("data", [])
    if args.limit is not None:
        rows = rows[: args.limit]
    _emit(
        {
            "run": _run_record(run),
            "name": args.name,
            "path": remote_path,
            "columns": columns,
            "rows": rows,
        }
    )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entity", default=DEFAULT_ENTITY)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--timeout", type=_positive_int, default=30)
    subparsers = parser.add_subparsers(dest="command", required=True)

    runs = subparsers.add_parser("runs", help="list recent run identities")
    runs.add_argument("--limit", type=_positive_int, default=20)
    runs.add_argument("--state", action="append", default=[])
    runs.add_argument("--name-contains")
    runs.add_argument("--since-hours", type=float)
    runs.set_defaults(func=_cmd_runs)

    summary = subparsers.add_parser("summary", help="read selected summary metrics")
    summary.add_argument("--run", action="append", required=True)
    summary.add_argument("--key", action="append", default=[])
    summary.add_argument("--prefix", action="append", default=[])
    summary.set_defaults(func=_cmd_summary)

    history = subparsers.add_parser(
        "history", help="read a bounded metric history tail"
    )
    history.add_argument("--run", required=True)
    history.add_argument("--key", action="append", required=True)
    history.add_argument("--tail", type=_positive_int, default=20)
    history.add_argument("--lookback-steps", type=_positive_int, default=2_000)
    history.add_argument("--page-size", type=_positive_int, default=1_000)
    history.set_defaults(func=_cmd_history)

    table = subparsers.add_parser("table", help="read the current named table artifact")
    table.add_argument("--run", required=True)
    table.add_argument("--name", required=True)
    table.add_argument("--limit", type=_positive_int)
    table.set_defaults(func=_cmd_table)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if getattr(args, "since_hours", None) is not None and args.since_hours <= 0:
        raise SystemExit("--since-hours must be positive")
    args.func(args)


if __name__ == "__main__":
    main()
