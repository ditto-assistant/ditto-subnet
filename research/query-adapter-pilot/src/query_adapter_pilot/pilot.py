"""Normal pilot entry point: provision, process a synthetic workload, report.

    python -m query_adapter_pilot run --workload PATH --miner-url URL \\
        --token-file PATH --out PATH [--run-id ID] [--salt S] \\
        [--verify-rate 1.0] [--fallback local|identity] [--timeout-ms 250]

Exit 0 when the run completes, even if every request fell back; exit 2 on
usage or configuration errors (bad workload, non-loopback miner URL, missing
token file). The report is for analysis only: it records usage, never payouts.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import numpy as np

from .client import PilotClient, RequestResult
from .contract import CONTRACT_VERSION, Outcome, Source
from .workload import Workload

REPORT_KIND: Final = "pilot-report"
DEFAULT_SALT: Final = "query-adapter-pilot/v1"
RUN_ID_RE: Final = re.compile(r"^[a-z0-9-]{1,32}$")
EXIT_OK: Final = 0
EXIT_USAGE: Final = 2


class UsageError(Exception):
    """A usage or configuration error (exit 2)."""


def new_run_id() -> str:
    """Short random lowercase id that fits the request-id run segment."""
    return "r" + secrets.token_hex(5)


def _pct(values: Sequence[float], q: float) -> float | None:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q)) if values else None


def summarize(results: Sequence[RequestResult]) -> dict[str, Any]:
    ok = [r.latency_ms for r in results if r.outcome == Outcome.OK]
    e2e = [r.latency_ms for r in results]
    return {
        "total": len(results),
        "by_outcome": dict(sorted(Counter(r.outcome for r in results).items())),
        "by_source": dict(sorted(Counter(r.source for r in results).items())),
        "latency_ms": {
            "miner_ok_p50": _pct(ok, 50),
            "miner_ok_p95": _pct(ok, 95),
            "end_to_end_p50": _pct(e2e, 50),
            "end_to_end_p95": _pct(e2e, 95),
        },
        "parity": {
            "min_reference_cos": min((r.reference_cos for r in results), default=None),
            "max_reference_abs": max((r.reference_max_abs for r in results), default=None),
        },
    }


def run_pilot(client: PilotClient, workload: Workload) -> dict[str, Any]:
    """Provision every user, process requests in order, and build the report."""
    provision = client.provision(workload)
    results = [client.adapt(spec) for spec in workload.requests]
    return {
        "contract": CONTRACT_VERSION,
        "kind": REPORT_KIND,
        "run_id": client.run_id,
        "workload": workload.name,
        "miner_url": client.miner_url,
        "config": {
            "verify_rate": client.verify_rate,
            "fallback": client.fallback,
            "timeout_ms": client.timeout_s * 1e3,
        },
        "provision": provision,
        "requests": [r.to_report() for r in results],
        "summary": summarize(results),
        "ledger": client.ledger.to_dict(),
    }


def summary_line(report: dict[str, Any], out: Path) -> str:
    s = report["summary"]
    by_source = s["by_source"]
    fallbacks = sum(n for k, n in by_source.items() if k != Source.MINER)
    ledger = report["ledger"]
    p50 = s["latency_ms"]["end_to_end_p50"]
    return (
        f"query-adapter pilot {report['run_id']} ({report['workload']}): "
        f"{s['total']} requests, miner={by_source.get(Source.MINER, 0)} "
        f"fallback={fallbacks}, units={ledger['accepted_units']} "
        f"duplicates_suppressed={ledger['duplicates_suppressed']}, "
        f"e2e_p50_ms={'n/a' if p50 is None else f'{p50:.3f}'}, "
        f"min_reference_cos={s['parity']['min_reference_cos']}, payouts=none -> {out}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="query-adapter-pilot",
        description="Synthetic-only query-adapter/v1 hosting pilot (no payouts).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="provision a loopback miner and run a synthetic workload")
    run.add_argument("--workload", required=True, type=Path)
    run.add_argument("--miner-url", required=True)
    run.add_argument("--token-file", required=True, type=Path)
    run.add_argument("--out", required=True, type=Path)
    run.add_argument("--run-id", default=None)
    run.add_argument("--salt", default=DEFAULT_SALT)
    run.add_argument("--verify-rate", type=float, default=1.0)
    run.add_argument("--fallback", choices=("local", "identity"), default="local")
    run.add_argument("--timeout-ms", type=float, default=250.0)
    return parser


def _configure(args: argparse.Namespace) -> tuple[PilotClient, Workload]:
    try:
        workload = Workload.load(args.workload)
    except (OSError, ValueError) as exc:
        raise UsageError(f"bad workload {args.workload}: {exc}") from exc
    try:
        token = args.token_file.read_text().strip()
    except OSError as exc:
        raise UsageError(f"cannot read token file {args.token_file}: {exc}") from exc
    if not token:
        raise UsageError(f"token file {args.token_file} is empty")
    run_id = args.run_id if args.run_id is not None else new_run_id()
    if not RUN_ID_RE.fullmatch(run_id):
        raise UsageError(f"--run-id {run_id!r} must match {RUN_ID_RE.pattern}")
    if not args.timeout_ms > 0:
        raise UsageError("--timeout-ms must be positive")
    try:
        client = PilotClient(
            args.miner_url,
            token,
            run_id,
            salt=args.salt,
            timeout_s=args.timeout_ms / 1e3,
            verify_rate=args.verify_rate,
            fallback=args.fallback,
        )
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    return client, workload


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        client, workload = _configure(args)
    except UsageError as exc:
        print(f"query-adapter-pilot: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    report = run_pilot(client, workload)
    out: Path = args.out
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    except OSError as exc:
        print(f"query-adapter-pilot: error: cannot write {out}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(summary_line(report, out))
    return EXIT_OK
