#!/usr/bin/env python3
"""SHA-bound offline calibration; no Platform access or production mutations."""

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from ditto_screener.calibration import classification_metrics
from ditto_screener.fanout_review import MODEL, review_archive
from ditto_screening_protocol import SCREENING_POLICY_VERSION


def write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".fanout-")
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, indent=2, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--api-key-file", required=True, type=Path)
    parser.add_argument("--results-file", required=True, type=Path)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument(
        "--partition", choices=("files", "specialists", "hybrid"), default="hybrid"
    )
    parser.add_argument("--files-per-group", type=int, default=4)
    parser.add_argument("--group-bytes", type=int, default=64_000)
    parser.add_argument("--max-groups", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--policy-version", type=int, default=SCREENING_POLICY_VERSION)
    args = parser.parse_args()
    items = json.loads(args.manifest.read_text())["items"]
    if not items:
        raise ValueError("manifest must contain items")
    root = args.artifact_root.resolve()
    cases = []
    for item in items:
        if item["expected_disposition"] not in {"safe", "violation"}:
            raise ValueError("expected_disposition must be safe or violation")
        archive = (root / item["archive"]).resolve()
        if not archive.is_relative_to(root):
            raise ValueError("archive must stay inside artifact-root")
        cases.append((item, archive))
    results = []
    for item, archive in cases:
        result = await review_archive(
            archive,
            artifact_sha256=item["artifact_sha256"],
            api_key_file=str(args.api_key_file),
            model=args.model,
            partition=args.partition,
            files_per_group=args.files_per_group,
            group_bytes=args.group_bytes,
            max_groups=args.max_groups,
            concurrency=args.concurrency,
            max_steps=args.max_steps,
            timeout_seconds=args.timeout_seconds,
            policy_version=args.policy_version,
        )
        result["expected_disposition"] = item["expected_disposition"]
        results.append(result)
        metrics = {}
        for strategy in ("baseline", "fanout", "critic"):
            rows = []
            for row in results:
                positive = {
                    "baseline": row["baseline_outcome"] == "candidate",
                    "fanout": bool(row["candidates"]),
                    "critic": row["outcome"] in {"candidate", "critic_also_flagged"},
                }[strategy]
                rows.append(
                    {
                        "expected_disposition": row["expected_disposition"],
                        "actual_disposition": "violation"
                        if positive
                        else "inconclusive",
                    }
                )
            metrics[strategy] = classification_metrics(rows)
        write_private(
            args.results_file,
            {
                "items": results,
                "metrics": metrics,
                "completed": len(results),
                "total": len(cases),
            },
        )


if __name__ == "__main__":
    asyncio.run(main())
