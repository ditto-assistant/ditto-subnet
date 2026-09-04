"""Command-line interface for the shadow coding datagen."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from dittobench_coding_datagen.audit import audit_curation_seed
from dittobench_coding_datagen.canonical import canonical_json_bytes
from dittobench_coding_datagen.compiler import compile_practice, grade, materialize
from dittobench_coding_datagen.model import CorpusError
from dittobench_coding_datagen.practice_server import evaluate_practice_harness
from dittobench_coding_datagen.public_controls import (
    PUBLIC_CONDITIONS,
    validate_public_task_controls,
)
from dittobench_coding_datagen.public_release import (
    build_public_practice_release,
    verify_public_practice_release,
)
from dittobench_coding_datagen.public_result_runner import aggregate_public_v2_results
from dittobench_coding_datagen.snapshot_archive import (
    build_snapshot_archive,
    verify_snapshot_archive,
)
from dittobench_coding_datagen.validation import validate_pack


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dittobench-coding-datagen")
    subcommands = parser.add_subparsers(dest="command", required=True)

    compile_parser = subcommands.add_parser(
        "compile-practice", help="compile a canonical public practice pack"
    )
    compile_parser.add_argument("--source", type=Path, required=True)
    compile_parser.add_argument("--output", type=Path, required=True)
    compile_parser.add_argument("--replace", action="store_true")

    validate_parser = subcommands.add_parser(
        "validate-pack", help="verify a compiled public practice pack"
    )
    validate_parser.add_argument("pack", type=Path)

    materialize_parser = subcommands.add_parser(
        "materialize", help="create one editable public practice workspace"
    )
    materialize_parser.add_argument("--pack", type=Path, required=True)
    materialize_parser.add_argument("--task", required=True)
    materialize_parser.add_argument("--output", type=Path, required=True)

    grade_parser = subcommands.add_parser(
        "grade", help="grade one public practice workspace in a disposable copy"
    )
    grade_parser.add_argument("--pack", type=Path, required=True)
    grade_parser.add_argument("--task", required=True)
    grade_parser.add_argument("--workspace", type=Path, required=True)
    grade_parser.add_argument("--timeout-seconds", type=int, default=30)

    evaluate_parser = subcommands.add_parser(
        "evaluate-practice",
        help="evaluate a loopback coding harness against one public practice task",
    )
    evaluate_parser.add_argument("--pack", type=Path, required=True)
    evaluate_parser.add_argument("--task", required=True)
    evaluate_parser.add_argument("--harness-url", required=True)
    evaluate_parser.add_argument(
        "--inference-base-url",
        default="http://127.0.0.1:9/offline-disabled",
    )
    evaluate_parser.add_argument("--timeout-seconds", type=int, default=120)

    audit_parser = subcommands.add_parser(
        "audit-curation",
        help="audit an external flat curation seed without importing it",
    )
    audit_parser.add_argument("root", type=Path)
    audit_parser.add_argument("--output", type=Path)

    release_parser = subcommands.add_parser(
        "build-public-release",
        help="build a deterministic public practice archive and release descriptor",
    )
    release_parser.add_argument("--pack", type=Path, required=True)
    release_parser.add_argument("--output", type=Path, required=True)
    release_parser.add_argument("--replace", action="store_true")

    verify_release_parser = subcommands.add_parser(
        "verify-public-release",
        help="verify a public practice archive against its release descriptor",
    )
    verify_release_parser.add_argument("--archive", type=Path, required=True)
    verify_release_parser.add_argument("--descriptor", type=Path, required=True)

    summarize_parser = subcommands.add_parser(
        "summarize-public-practice",
        help="aggregate ten public v2 task results into one non-authoritative report",
    )
    summarize_parser.add_argument("--pack", type=Path, required=True)
    summarize_parser.add_argument("--harness-artifact-sha256", required=True)
    summarize_parser.add_argument(
        "--task-result", type=Path, action="append", required=True
    )
    summarize_parser.add_argument("--output", type=Path)

    snapshot_archive_parser = subcommands.add_parser(
        "build-snapshot-archive",
        help="build one deterministic archive from a sanitized snapshot",
    )
    snapshot_archive_parser.add_argument("--snapshot", type=Path, required=True)
    snapshot_archive_parser.add_argument("--archive", type=Path, required=True)
    snapshot_archive_parser.add_argument("--replace", action="store_true")

    verify_snapshot_parser = subcommands.add_parser(
        "verify-snapshot-archive",
        help="verify one deterministic sanitized snapshot archive",
    )
    verify_snapshot_parser.add_argument("--archive", type=Path, required=True)

    controls_parser = subcommands.add_parser(
        "validate-public-controls",
        help="validate one external public v2 task control set",
    )
    controls_parser.add_argument("--task-root", type=Path, required=True)
    controls_parser.add_argument("--task-id", required=True)
    controls_parser.add_argument(
        "--condition", choices=sorted(PUBLIC_CONDITIONS), required=True
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "compile-practice":
            result = compile_practice(args.source, args.output, replace=args.replace)
            print(canonical_json_bytes(result).decode("utf-8"), end="")
            return 0
        if args.command == "validate-pack":
            result = validate_pack(args.pack)
            print(canonical_json_bytes(result).decode("utf-8"), end="")
            return 0
        if args.command == "materialize":
            output = materialize(args.pack, args.task, args.output)
            print(output)
            return 0
        if args.command == "grade":
            if args.timeout_seconds < 1 or args.timeout_seconds > 300:
                raise CorpusError("timeout-seconds must be between 1 and 300")
            return grade(
                args.pack,
                args.task,
                args.workspace,
                timeout_seconds=args.timeout_seconds,
            )
        if args.command == "evaluate-practice":
            evidence = evaluate_practice_harness(
                args.pack,
                args.task,
                args.harness_url,
                inference_base_url=args.inference_base_url,
                timeout_seconds=args.timeout_seconds,
            )
            print(canonical_json_bytes(evidence.as_json()).decode("utf-8"), end="")
            return 0 if evidence.repair_score_micros == 1_000_000 else 1
        if args.command == "audit-curation":
            result = audit_curation_seed(args.root)
            body = canonical_json_bytes(result)
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_bytes(body)
            print(body.decode("utf-8"), end="")
            return 1 if result["status"] == "BLOCKED" else 0
        if args.command == "build-public-release":
            result = build_public_practice_release(
                args.pack,
                args.output,
                replace=args.replace,
            )
            print(canonical_json_bytes(result).decode("utf-8"), end="")
            return 0
        if args.command == "verify-public-release":
            result = verify_public_practice_release(
                archive=args.archive,
                descriptor=args.descriptor,
            )
            print(canonical_json_bytes(result).decode("utf-8"), end="")
            return 0
        if args.command == "summarize-public-practice":
            result_bytes = aggregate_public_v2_results(
                pack=args.pack,
                harness_artifact_sha256=args.harness_artifact_sha256,
                task_result_paths=tuple(args.task_result),
            )
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_bytes(result_bytes)
            print(result_bytes.decode("utf-8"), end="")
            return 0
        if args.command == "build-snapshot-archive":
            receipt = build_snapshot_archive(
                snapshot=args.snapshot,
                archive=args.archive,
                replace=args.replace,
            )
            print(canonical_json_bytes(receipt.as_json()).decode("utf-8"), end="")
            return 0
        if args.command == "verify-snapshot-archive":
            receipt = verify_snapshot_archive(args.archive)
            print(canonical_json_bytes(receipt.as_json()).decode("utf-8"), end="")
            return 0
        if args.command == "validate-public-controls":
            authority = validate_public_task_controls(
                task_root=args.task_root,
                task_id=args.task_id,
                condition=args.condition,
                workspace=args.task_root / "snapshot" / "workspace",
            )
            print(canonical_json_bytes(authority.as_json()).decode("utf-8"), end="")
            return 0
        raise CorpusError(f"unknown command: {args.command}")
    except CorpusError as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
