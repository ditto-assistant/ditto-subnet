"""``dittobench-reranker``: the same scorer validators run, for miners too.

    dittobench-reranker validate ARTIFACT
    dittobench-reranker score --artifact ARTIFACT --pools pools.json [--out r.json]
    dittobench-reranker latency --artifact ARTIFACT --pools pools.json [--incumbent DIR]
    dittobench-reranker compare champion.json challenger.json
    dittobench-reranker pack ARTIFACT_DIR out.tar.gz

ARTIFACT is a directory or tarball holding model.onnx, vocab.txt, manifest.json.
Pools come from ``poolgen/cmd/rerank-pools``. Scores on seeds you choose are
practice; scored seeds are fixed by Platform after you commit.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from dittobench_reranker.artifact import ArtifactError, load_artifact, pack
from dittobench_reranker.contract import CONTRACT
from dittobench_reranker.pools import PoolFormatError, load_bundle
from dittobench_reranker.scorer import (
    PoolResult,
    ScoreReport,
    ScoringError,
    compare,
    measure_latency,
    score,
)


def _report_from_dict(d: dict) -> ScoreReport:
    d = dict(d)
    d["per_pool"] = tuple(PoolResult(**p) for p in d["per_pool"])
    return ScoreReport(**d)


def _emit(data: dict, out: str | None) -> None:
    text = json.dumps(data, indent=2, sort_keys=True)
    if out:
        Path(out).write_text(text + "\n")
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dittobench-reranker",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="static artifact checks")
    v.add_argument("artifact")
    s = sub.add_parser("score", help="deterministic quality score")
    s.add_argument("--artifact", required=True)
    s.add_argument("--pools", required=True)
    s.add_argument("--out")
    s.add_argument("--summary", action="store_true", help="omit per-pool results")
    lat = sub.add_parser("latency", help="p50 latency against the contract budget")
    lat.add_argument("--artifact", required=True)
    lat.add_argument("--pools", required=True)
    lat.add_argument(
        "--incumbent",
        help="incumbent artifact; when given, the gate is the p50 ratio to it",
    )
    c = sub.add_parser("compare", help="paired dethrone test between two reports")
    c.add_argument("champion")
    c.add_argument("challenger")
    p = sub.add_parser("pack", help="deterministic upload tarball")
    p.add_argument("directory")
    p.add_argument("out")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "validate":
            art = load_artifact(args.artifact)
            _emit(
                {
                    "ok": True,
                    "artifact_sha256": art.sha256,
                    "warnings": list(art.warnings),
                },
                None,
            )
        elif args.cmd == "score":
            art = load_artifact(args.artifact)
            bundle = load_bundle(args.pools, CONTRACT.scoring.pool_size)
            data = score(art, bundle).to_dict()
            if args.summary:
                data.pop("per_pool")
            _emit(data, args.out)
        elif args.cmd == "latency":
            art = load_artifact(args.artifact)
            bundle = load_bundle(args.pools, CONTRACT.scoring.pool_size)
            incumbent = load_artifact(args.incumbent) if args.incumbent else None
            report = measure_latency(art, bundle, incumbent=incumbent)
            _emit(asdict(report), None)
            return 0 if report.passed else 3
        elif args.cmd == "compare":
            champ = _report_from_dict(json.loads(Path(args.champion).read_text()))
            chall = _report_from_dict(json.loads(Path(args.challenger).read_text()))
            _emit(asdict(compare(champ, chall)), None)
        elif args.cmd == "pack":
            load_artifact(args.directory)
            Path(args.out).write_bytes(pack(args.directory))
            print(args.out)
    except (ArtifactError, PoolFormatError, ScoringError, ValueError) as exc:
        print(f"dittobench-reranker: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
