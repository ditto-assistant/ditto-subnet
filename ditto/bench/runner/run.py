"""CLI entry point for ``python -m ditto.bench.runner``.

Loads public fixtures, drives a miner harness Docker image over stdio,
scores responses with the Python port of the canonical scorer, and writes a
report JSON file. This is the contributor-facing fast-feedback loop; the
on-chain validator runs the same protocol with stricter sandboxing.

Example::

    python -m ditto.bench.runner \\
      --image my-miner-harness:dev \\
      --mechanism ditto_core \\
      --visibility public \\
      --sample 10 \\
      --report out/report.json
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ditto.bench import SCHEMA_VERSION
from ditto.bench.loader.cases import (
    RetrievalCase,
    ToolCallCase,
    load_retrieval_cases,
    load_toolcall_cases,
)
from ditto.bench.loader.taxonomy import Mechanism
from ditto.bench.runner.antigaming import HiddenSet, partition_fixture
from ditto.bench.runner.docker import HarnessConfig, HarnessDriver, HarnessTimeoutError
from ditto.bench.runner.report import write_report
from ditto.bench.runner.scoring import (
    CoreScoreInputs,
    RetrievalScore,
    RetrievalScoreInputs,
    Score,
    ToolCallScore,
    score_core,
    score_retrieval,
)


def _fixtures_root() -> Path:
    """Return the path to the bundled ``ditto/bench/fixtures`` directory."""
    return Path(__file__).resolve().parent.parent / "fixtures"


def _build_core_request(
    case: ToolCallCase, validator_seed: str, deadline_ms: int
) -> dict[str, Any]:
    """Build a ChallengeRequest dict for a DittoCore case."""
    return {
        "schema_version": SCHEMA_VERSION,
        "challenge_id": uuid.uuid4().hex,
        "mechanism": str(Mechanism.CORE),
        "case_id": case.id,
        "category": case.category,
        "domain": case.domain,
        "prompt": case.prompt,
        "tool_schemas": [],
        "stm_context": [],
        "validator_seed": validator_seed,
        "issued_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "deadline_ms": deadline_ms,
    }


def _build_retrieval_request(
    case: RetrievalCase, validator_seed: str, deadline_ms: int
) -> dict[str, Any]:
    """Build a ChallengeRequest dict for a DittoRetrieval case."""
    return {
        "schema_version": SCHEMA_VERSION,
        "challenge_id": uuid.uuid4().hex,
        "mechanism": str(Mechanism.RETRIEVAL),
        "case_id": case.id,
        "category": case.category,
        "query": case.query,
        "k": case.k,
        "user_fixture_id": case.user_fixture_id,
        "include_answer": False,
        "validator_seed": validator_seed,
        "issued_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "deadline_ms": deadline_ms,
    }


def _score_core_response(
    case: ToolCallCase, response: dict[str, Any], budget_latency_ms: int
) -> Score:
    """Contributor scorer: accepts a harness-supplied ``tool_score`` envelope.

    The full multiset-F1 + arg-matcher pipeline that production validators
    run is intentionally not duplicated in Python; it lives in the
    closed-source validator binary. The contributor runner here is
    fast-feedback only: it accepts a ``tool_score`` envelope from the
    harness when present (so miners can self-score with their own
    pipeline) and falls back to a naive name-only multiset match otherwise.
    Either way the composite math in :func:`score_core` matches the
    canonical Go scorer at ``go/bittensor/scoring.go`` byte-for-byte.
    """
    tool_score = response.get("tool_score") or {}
    expected_names = [t.name for t in case.expected_tools]
    observed_names = [c.get("name", "") for c in (response.get("tool_calls") or [])]

    if tool_score:
        ts = ToolCallScore(
            name_f1=float(tool_score.get("name_f1", 0.0)),
            arg_f1=float(tool_score.get("arg_f1", 0.0)),
            trajectory_penalty=float(tool_score.get("trajectory_penalty", 0.0)),
            abstain_correct=bool(tool_score.get("abstain_correct", False)),
        )
    else:
        ts = _naive_tool_score(expected_names, observed_names)

    return score_core(
        CoreScoreInputs(
            case=case,
            tool=ts,
            latency_ms=int(response.get("total_latency_ms", 0) or 0),
            budget_latency_ms=budget_latency_ms,
        )
    )


def _naive_tool_score(expected: list[str], observed: list[str]) -> ToolCallScore:
    """Tiny multiset-F1 implementation for the contributor stub.

    Production validators replace this with their own argument-matcher
    pipeline; the composite math in :func:`score_core` does not depend on
    how ``name_f1`` and ``arg_f1`` are computed, so the stub is sufficient
    for local feedback during harness development.
    """
    if not expected and not observed:
        return ToolCallScore(name_f1=0.0, arg_f1=1.0, abstain_correct=True)
    if not expected:
        return ToolCallScore(name_f1=0.0, arg_f1=0.0, abstain_correct=False)
    exp_set = set(expected)
    obs_set = set(observed)
    tp = len(exp_set & obs_set)
    precision = tp / len(obs_set) if obs_set else 0.0
    recall = tp / len(exp_set) if exp_set else 0.0
    f1 = (
        (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    )
    return ToolCallScore(name_precision=precision, name_recall=recall, name_f1=f1)


def _score_retrieval_response(
    case: RetrievalCase, response: dict[str, Any], budget_latency_ms: int
) -> Score:
    """Score one retrieval response using harness-emitted IR metrics if present.

    Mirrors the contributor-stub semantics of :func:`_score_core_response`.
    """
    rs_envelope = response.get("retrieval_score") or {}
    if rs_envelope:
        rs = RetrievalScore(
            ndcg_5=float(rs_envelope.get("ndcg_5", 0.0)),
            mrr=float(rs_envelope.get("mrr", 0.0)),
            recall_5=float(rs_envelope.get("recall_5", 0.0)),
            needle_hit=bool(rs_envelope.get("needle_hit", False)),
            abstain_correct=bool(rs_envelope.get("abstain_correct", False)),
            contradiction_pass=bool(rs_envelope.get("contradiction_pass", False)),
        )
    else:
        rs = _naive_retrieval_score(case, response.get("evidence_ids") or [])

    return score_retrieval(
        RetrievalScoreInputs(
            case=case,
            retrieval=rs,
            latency_ms=int(response.get("total_latency_ms", 0) or 0),
            budget_latency_ms=budget_latency_ms,
        )
    )


def _naive_retrieval_score(
    case: RetrievalCase, evidence_ids: list[str]
) -> RetrievalScore:
    """Stub recall@K + needle-hit calc when the harness doesn't supply metrics."""
    expected = set(case.expected_pair_ids)
    forbidden = set(case.forbidden_pair_ids)
    if not expected:
        return RetrievalScore(abstain_correct=not evidence_ids)
    top5 = evidence_ids[:5]
    hits5 = expected & set(top5)
    recall5 = len(hits5) / len(expected)
    needle_hit = expected.issubset(set(evidence_ids[: case.k or 10]))
    forbidden_hit = len(set(evidence_ids) & forbidden)
    mrr = 0.0
    for rank, pid in enumerate(evidence_ids, start=1):
        if pid in expected:
            mrr = 1.0 / rank
            break
    return RetrievalScore(
        ndcg_5=recall5,
        mrr=mrr,
        recall_5=recall5,
        needle_hit=needle_hit,
        num_forbidden_hit=forbidden_hit,
        contradiction_pass=forbidden_hit == 0,
    )


def _filter_visibility(
    cases: list[Any], visibility: str, *, secret: str | None
) -> list[Any]:
    """Return only cases whose visibility bucket matches ``visibility``.

    When ``visibility == 'all'`` every case is returned. Otherwise the
    filter consults :func:`partition_fixture` against ``secret`` so the
    public / private / canary splits match the on-chain validator's view
    rather than trusting the on-disk ``visibility`` field (which is only
    stamped after grading). ``secret`` is the validator-controlled string
    documented in ``ditto/bench/docs/anti_gaming.md``; for the contributor
    runner it defaults to the CLI ``--seed`` value.
    """
    if visibility == "all":
        return cases
    if not cases:
        return []
    if secret is None:
        return [c for c in cases if (c.visibility or "public") == visibility]
    bucket = _bucket_for(cases, secret)
    keep = {
        "public": set(bucket.public),
        "private": set(bucket.private),
        "canary": set(bucket.canary),
    }[visibility]
    return [c for c in cases if c.id in keep]


def _bucket_for(cases: list[Any], secret: str) -> HiddenSet:
    """Run :func:`partition_fixture` over ``cases`` with conservative fractions.

    Defaults (private 25%, canary 15%) match the example fractions in
    ``docs/anti_gaming.md``. Validators are expected to override these via
    their own configuration; the contributor runner only needs reasonable
    defaults so ``--visibility private|canary`` returns a non-empty subset.
    """
    return partition_fixture(
        [c.id for c in cases],
        secret,
        private_frac=0.25,
        canary_frac=0.15,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    p = argparse.ArgumentParser(
        prog="ditto.bench.runner",
        description="Drive a miner harness OCI image over DittoBench's stdio protocol.",
    )
    p.add_argument(
        "--image",
        required=True,
        help="Docker image to launch (preferably pinned by digest).",
    )
    p.add_argument(
        "--mechanism",
        choices=[str(m) for m in Mechanism] + ["all"],
        default="all",
        help="Mechanism to score; defaults to all available.",
    )
    p.add_argument(
        "--visibility",
        choices=["public", "private", "canary", "all"],
        default="public",
        help="Fixture visibility split to score (defaults to public).",
    )
    p.add_argument(
        "--sample",
        type=int,
        default=0,
        help="Random sample size (per mechanism); 0 = all cases.",
    )
    p.add_argument(
        "--seed",
        default="local-run",
        help="Validator seed echoed into each ChallengeRequest.",
    )
    p.add_argument(
        "--deadline-ms",
        type=int,
        default=8000,
        help="Per-case wall-clock budget in milliseconds (default 8000).",
    )
    p.add_argument(
        "--budget-latency-ms",
        type=int,
        default=4000,
        help="Latency score budget (decays linearly to 0 at 5x).",
    )
    p.add_argument(
        "--fixtures",
        type=Path,
        default=None,
        help="Override fixtures root (defaults to the bundled ditto/bench/fixtures).",
    )
    p.add_argument(
        "--report",
        type=Path,
        default=Path("out/report.json"),
        help="Where to write the JSON report (default out/report.json).",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (-v info, -vv debug).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; returns a process exit code."""
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level={0: logging.WARNING, 1: logging.INFO}.get(args.verbose, logging.DEBUG),
        format="%(levelname)s %(name)s: %(message)s",
    )

    fixtures_root = args.fixtures or _fixtures_root()
    rng = random.Random(args.seed)

    core_cases: list[ToolCallCase] = []
    retrieval_cases: list[RetrievalCase] = []
    if args.mechanism in (str(Mechanism.CORE), "all"):
        core_cases = _filter_visibility(
            load_toolcall_cases(fixtures_root / "toolcall"),
            args.visibility,
            secret=args.seed,
        )
        if args.sample > 0:
            core_cases = rng.sample(core_cases, k=min(args.sample, len(core_cases)))
    if args.mechanism in (str(Mechanism.RETRIEVAL), "all"):
        retrieval_cases = _filter_visibility(
            load_retrieval_cases(fixtures_root / "retrieval"),
            args.visibility,
            secret=args.seed,
        )
        if args.sample > 0:
            retrieval_cases = rng.sample(
                retrieval_cases, k=min(args.sample, len(retrieval_cases))
            )

    config = HarnessConfig(image=args.image)
    scores: list[Score] = []
    with HarnessDriver(config) as harness:
        for case in core_cases:
            req = _build_core_request(case, args.seed, args.deadline_ms)
            try:
                resp = harness.send(req, deadline_ms=args.deadline_ms)
            except HarnessTimeoutError:
                scores.append(
                    _zero_score(case.id, Mechanism.CORE, case.category, case.domain)
                )
                continue
            scores.append(_score_core_response(case, resp, args.budget_latency_ms))

        for rcase in retrieval_cases:
            req = _build_retrieval_request(rcase, args.seed, args.deadline_ms)
            try:
                resp = harness.send(req, deadline_ms=args.deadline_ms)
            except HarnessTimeoutError:
                scores.append(
                    _zero_score(rcase.id, Mechanism.RETRIEVAL, rcase.category, "")
                )
                continue
            scores.append(
                _score_retrieval_response(rcase, resp, args.budget_latency_ms)
            )

    report = write_report(scores, image=args.image, out_path=args.report)
    for agg in report.aggregates:
        sys.stdout.write(f"{agg.mechanism}: n={agg.count} mean={agg.mean_score:.3f}\n")
    return 0


def _zero_score(
    case_id: str, mechanism: Mechanism, category: str, domain: str
) -> Score:
    """Construct a zero-scored record for a case that timed out / refused."""
    return Score(
        schema_version=SCHEMA_VERSION,
        case_id=case_id,
        mechanism=mechanism,
        score=0.0,
        category=category,
        domain=domain,
        notes=["timeout_or_refusal"],
    )


if __name__ == "__main__":
    sys.exit(main())
