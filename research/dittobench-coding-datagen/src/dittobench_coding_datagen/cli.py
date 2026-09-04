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
from dittobench_coding_datagen.private_audit import audit_private_group_inputs
from dittobench_coding_datagen.private_authoring import (
    build_private_group_from_source,
    load_private_group_manifest,
    write_private_authoring_output,
)
from dittobench_coding_datagen.private_release import (
    compile_private_release,
    load_private_release,
)
from dittobench_coding_datagen.public_controls import (
    PUBLIC_CONDITIONS,
    validate_public_task_controls,
)
from dittobench_coding_datagen.public_pack_v2 import (
    compile_public_v2_pack,
    validate_public_v2_pack,
)
from dittobench_coding_datagen.public_release import (
    build_public_practice_release,
    verify_public_practice_release,
)
from dittobench_coding_datagen.public_result_runner import aggregate_public_v2_results
from dittobench_coding_datagen.public_task_runner import (
    run_public_v2_controls,
    run_public_v2_task,
)
from dittobench_coding_datagen.public_v2_publish_plan import (
    build_public_v2_publish_plan,
    canonical_public_v2_publish_plan_bytes,
)
from dittobench_coding_datagen.public_v2_release import (
    build_public_v2_release,
    verify_public_v2_release,
)
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

    task_runner_parser = subcommands.add_parser(
        "run-public-task",
        help="grade one local workspace against a public v2 task",
    )
    task_runner_parser.add_argument("--pack", type=Path, required=True)
    task_runner_parser.add_argument("--task", required=True)
    task_runner_parser.add_argument("--workspace", type=Path, required=True)
    task_runner_parser.add_argument("--image", required=True)
    task_runner_parser.add_argument("--output", type=Path)

    controls_runner_parser = subcommands.add_parser(
        "run-public-controls",
        help="grade one external public v2 curator control set",
    )
    controls_runner_parser.add_argument("--task-root", type=Path, required=True)
    controls_runner_parser.add_argument("--task", required=True)
    controls_runner_parser.add_argument(
        "--condition", choices=sorted(PUBLIC_CONDITIONS), required=True
    )
    controls_runner_parser.add_argument("--workspace", type=Path, required=True)
    controls_runner_parser.add_argument("--image", required=True)
    controls_runner_parser.add_argument("--output", type=Path)

    compile_v2_parser = subcommands.add_parser(
        "compile-public-v2-pack",
        help="compile verified external staging into a ten-task public v2 pack",
    )
    compile_v2_parser.add_argument("--staging-root", type=Path, required=True)
    compile_v2_parser.add_argument("--intake", type=Path, required=True)
    compile_v2_parser.add_argument("--output", type=Path, required=True)
    compile_v2_parser.add_argument("--replace", action="store_true")

    validate_v2_parser = subcommands.add_parser(
        "validate-public-v2-pack",
        help="verify a compiled ten-task public v2 pack",
    )
    validate_v2_parser.add_argument("pack", type=Path)

    release_v2_parser = subcommands.add_parser(
        "build-public-v2-release",
        help="build a deterministic public v2 archive and descriptor",
    )
    release_v2_parser.add_argument("--pack", type=Path, required=True)
    release_v2_parser.add_argument("--output", type=Path, required=True)
    release_v2_parser.add_argument("--replace", action="store_true")

    verify_release_v2_parser = subcommands.add_parser(
        "verify-public-v2-release",
        help="verify a public v2 archive and descriptor",
    )
    verify_release_v2_parser.add_argument("--archive", type=Path, required=True)
    verify_release_v2_parser.add_argument("--descriptor", type=Path, required=True)

    publish_plan_parser = subcommands.add_parser(
        "plan-public-v2-publication",
        help="build a credential-free immutable upload plan for a public v2 release",
    )
    publish_plan_parser.add_argument("--release-dir", type=Path, required=True)
    publish_plan_parser.add_argument("--dataset-repository", required=True)
    publish_plan_parser.add_argument("--revision", required=True)
    publish_plan_parser.add_argument("--output", type=Path, required=True)

    private_group_parser = subcommands.add_parser(
        "build-private-group",
        help="build one canonical private v2 group manifest from protected source",
    )
    private_group_parser.add_argument("--source", type=Path, required=True)
    private_group_parser.add_argument("--output", type=Path, required=True)

    private_audit_parser = subcommands.add_parser(
        "audit-private-group",
        help="audit one private group against protected visible and grader trees",
    )
    private_audit_parser.add_argument("--manifest", type=Path, required=True)
    private_audit_parser.add_argument("--visible-snapshot", type=Path, required=True)
    private_audit_parser.add_argument("--hidden-grader", type=Path, required=True)
    private_audit_parser.add_argument("--memory-bundles", type=Path, required=True)
    private_audit_parser.add_argument("--overlap-review-sha256", required=True)
    private_audit_parser.add_argument("--output", type=Path, required=True)

    private_release_parser = subcommands.add_parser(
        "compile-private-release",
        help="compile fifty audited private groups into one release authority",
    )
    private_release_parser.add_argument("--groups-dir", type=Path, required=True)
    private_release_parser.add_argument("--release-id", required=True)
    private_release_parser.add_argument("--output", type=Path, required=True)

    verify_private_release_parser = subcommands.add_parser(
        "verify-private-release",
        help="verify one canonical fifty-group private release authority",
    )
    verify_private_release_parser.add_argument("release", type=Path)
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
        if args.command == "run-public-task":
            task_result = run_public_v2_task(
                pack=args.pack,
                task_id=args.task,
                workspace=args.workspace,
                image=args.image,
            )
            body = canonical_json_bytes(task_result.as_json())
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_bytes(body)
            print(body.decode("utf-8"), end="")
            return 0 if task_result.resolved else 1
        if args.command == "run-public-controls":
            control_result = run_public_v2_controls(
                task_root=args.task_root,
                task_id=args.task,
                condition=args.condition,
                workspace=args.workspace,
                image=args.image,
            )
            body = canonical_json_bytes(control_result.as_json())
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_bytes(body)
            print(body.decode("utf-8"), end="")
            return 0 if control_result.resolved else 1
        if args.command == "compile-public-v2-pack":
            v2_manifest = compile_public_v2_pack(
                staging_root=args.staging_root,
                intake_path=args.intake,
                output=args.output,
                replace=args.replace,
            )
            print(canonical_json_bytes(v2_manifest).decode("utf-8"), end="")
            return 0
        if args.command == "validate-public-v2-pack":
            v2_manifest = validate_public_v2_pack(args.pack)
            print(canonical_json_bytes(v2_manifest).decode("utf-8"), end="")
            return 0
        if args.command == "build-public-v2-release":
            v2_descriptor = build_public_v2_release(
                pack=args.pack,
                output=args.output,
                replace=args.replace,
            )
            print(canonical_json_bytes(v2_descriptor).decode("utf-8"), end="")
            return 0
        if args.command == "verify-public-v2-release":
            v2_descriptor = verify_public_v2_release(
                archive=args.archive,
                descriptor=args.descriptor,
            )
            print(canonical_json_bytes(v2_descriptor).decode("utf-8"), end="")
            return 0
        if args.command == "plan-public-v2-publication":
            publish_plan = build_public_v2_publish_plan(
                release_dir=args.release_dir,
                dataset_repository=args.dataset_repository,
                revision=args.revision,
            )
            body = canonical_public_v2_publish_plan_bytes(publish_plan)
            if args.output.exists() or args.output.is_symlink():
                raise CorpusError("public v2 publication plan output already exists")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(body)
            args.output.chmod(0o644)
            print(body.decode("utf-8"), end="")
            return 0
        if args.command == "build-private-group":
            private_group = build_private_group_from_source(args.source)
            body = private_group.canonical_bytes()
            write_private_authoring_output(args.output, body)
            print(body.decode("utf-8"), end="")
            return 0
        if args.command == "audit-private-group":
            private_group = load_private_group_manifest(args.manifest)
            private_audit = audit_private_group_inputs(
                manifest=private_group,
                visible_snapshot=args.visible_snapshot,
                hidden_grader=args.hidden_grader,
                memory_bundles=args.memory_bundles,
                overlap_review_sha256=args.overlap_review_sha256,
            )
            body = private_audit.canonical_bytes()
            write_private_authoring_output(args.output, body)
            print(body.decode("utf-8"), end="")
            return 0
        if args.command == "compile-private-release":
            private_release = compile_private_release(
                groups_dir=args.groups_dir,
                corpus_release_id=args.release_id,
                output=args.output,
            )
            print(canonical_json_bytes(private_release).decode("utf-8"), end="")
            return 0
        if args.command == "verify-private-release":
            private_release = load_private_release(args.release)
            print(canonical_json_bytes(private_release).decode("utf-8"), end="")
            return 0
        raise CorpusError(f"unknown command: {args.command}")
    except CorpusError as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
