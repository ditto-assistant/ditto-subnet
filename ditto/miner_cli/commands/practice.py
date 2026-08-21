"""``ditto practice``: one-command local DittoBench and LongMemEval practice."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from ditto.miner_cli.errors import MinerCliError

REPO_ROOT = Path(__file__).resolve().parents[3]
REHEARSAL_SCRIPT = (
    REPO_ROOT / "miners" / "dittobench-starter-kit" / "scripts" / "local-rehearsal.py"
)


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "practice",
        help="Run a self-contained local DittoBench practice score.",
        description=(
            "Build and start the miner harness plus the local DittoBench scorer "
            "against the live SN118 scoring contract (currently bench 11). "
            "Optionally run the pinned LongMemEval-S adapter and official judge "
            "as a separate score. Nothing must be exposed to the public internet."
        ),
        parents=parents or [],
    )
    parser.add_argument(
        "--starter-kit",
        type=Path,
        help=(
            "Path to the miner starter-kit checkout to test. Defaults to this "
            "ditto-subnet checkout's miners/dittobench-starter-kit directory."
        ),
    )
    parser.add_argument(
        "--run-size",
        choices=("small", "medium", "full"),
        default="small",
        help=(
            "Dataset envelope: small smoke, medium, or full on-chain shape "
            "(default: small)."
        ),
    )
    parser.add_argument(
        "--bench-version",
        type=int,
        help=(
            "DittoBench contract to generate and score. Defaults to live SN118 "
            "scoring (11). cargo evaluate remains a separate local subset."
        ),
    )
    parser.add_argument("--seed", type=int, help="Pin the generated dataset seed.")
    parser.add_argument(
        "--timeout",
        type=int,
        default=7200,
        help="Maximum scoring time in seconds (default: 7200).",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Write the combined Bench and optional LongMemEval report as JSON.",
    )
    parser.add_argument(
        "--longmem-eval",
        action="store_true",
        help=(
            "After Bench scoring, run cleaned LongMemEval-S with the native memory "
            "tools and official GPT-4o judge. Reported separately from Bench."
        ),
    )
    parser.add_argument(
        "--longmem-limit",
        type=int,
        help=(
            "Run only the first N LongMemEval questions. Omit for the official "
            "500-question condition; limited runs are labeled partial practice."
        ),
    )
    parser.add_argument(
        "--longmem-shards",
        type=int,
        default=5,
        help="Number of isolated LongMemEval harness stores (default: 5).",
    )
    parser.set_defaults(func=run)
    return parser


def rehearsal_argv(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(REHEARSAL_SCRIPT),
        "--run-size",
        args.run_size,
        "--timeout",
        str(args.timeout),
    ]
    if args.starter_kit is not None:
        command.extend(("--starter-kit", str(args.starter_kit)))
    if args.bench_version is not None:
        command.extend(("--bench-version", str(args.bench_version)))
    if args.seed is not None:
        command.extend(("--seed", str(args.seed)))
    if args.report is not None:
        command.extend(("--report", str(args.report)))
    if args.longmem_eval:
        command.extend(("--longmem-eval", "--longmem-shards", str(args.longmem_shards)))
        if args.longmem_limit is not None:
            command.extend(("--longmem-limit", str(args.longmem_limit)))
    return command


def run(args: argparse.Namespace) -> int:
    if not REHEARSAL_SCRIPT.is_file():
        raise MinerCliError(
            "local practice assets are unavailable; run this command from a "
            "ditto-subnet source checkout"
        )
    completed = subprocess.run(rehearsal_argv(args), check=False)
    return int(completed.returncode)
