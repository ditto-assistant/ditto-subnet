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

    audit_parser = subcommands.add_parser(
        "audit-curation",
        help="audit an external flat curation seed without importing it",
    )
    audit_parser.add_argument("root", type=Path)
    audit_parser.add_argument("--output", type=Path)
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
        if args.command == "audit-curation":
            result = audit_curation_seed(args.root)
            body = canonical_json_bytes(result)
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_bytes(body)
            print(body.decode("utf-8"), end="")
            return 1 if result["status"] == "BLOCKED" else 0
        raise CorpusError(f"unknown command: {args.command}")
    except CorpusError as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
