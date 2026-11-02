---
name: ditto-subnet-benchmark
description: Implement, audit, or diagnose validator evaluation, DittoBench execution and v8 scoring, inference relays, live case concurrency, overlapping /run, benchmark datagen, miner starter-kit compatibility, screener protocols, calibration, and LongMemEval, Hermes, or OpenClaw adapters in the ditto-subnet monorepo. Use whenever scoring integrity, version negotiation, seeds, baselines, untrusted execution, adapters, research-to-production boundaries, slow benches, case_concurrency, 6600s timeouts, or concurrent scored cases matter.
---

# Ditto Subnet Benchmark

Protect production score semantics while allowing research and adapters to evolve beside them.

## Orient

```bash
python3 .agents/skills/ditto-subnet-context/scripts/lookup-context.py \
  --max-topics 4 "$ARGUMENTS"
```

Pass the user's task text verbatim. If it is empty, omit the query and begin
from the monorepo overview rather than injecting every benchmark owner.

Read [`references/benchmark-index.md`](references/benchmark-index.md), then the returned protocol and test anchors. For live overlapping `/run`, `case_concurrency`, ticket wall-clock, or 6600s timeouts, read [`references/bench-runtime.md`](references/bench-runtime.md).

To score a harness locally (composite, `tool_mean`, `memory_mean`, v12 gates),
use `$ditto-subnet-preview` and
[`../ditto-subnet-preview/references/localstack.md`](../ditto-subnet-preview/references/localstack.md).
Do not treat `uv run ditto practice` or cargo `evaluate` as that path.

## Generator competition

Generators emit challenge packs; each round freezes one bench version for the panel agents
and grades each column at its pack's declared `grading_bench_version`. Contract:
[`generator-competition.md`](../../../research/dittobench-datagen/docs/generator-competition.md)
("Bench contract"); scorer: `services/dittobench-api/docs/challenge-scorer.md`; v15 coupling:
[`dueling-bench.md`](../../../research/dittobench-datagen/docs/dueling-bench.md).

Every bench version change moves both competitions together — one version number, a
two-policy support window, rounds switching only behind the validator quorum gate. Before
touching round contracts, pack policies, or an activation, read
[`tandem-upgrades.md`](../../../research/dittobench-datagen/docs/tandem-upgrades.md) and use
`$ditto-subnet-bench-version-bump` (step 10 and "Activate (both competitions)").

## Evidence workflow

1. Identify the exact versioned contract and authoritative implementation before proposing a change.
2. Trace one job end to end: Platform lease, validator request, scorer execution, result envelope, signed submission, aggregation, and weight fold.
3. Separate production scoring from research adapters and calibration evidence.
4. Change producers, consumers, fixtures, generated contracts, and compatibility tests atomically.
5. Test the narrow package, then Go/Python integration and Compose identity checks.

## Integrity rules

- Do not change v8 scoring to make a provider, miner, adapter, or research result look better.
- Do not infer a scoring regression from aggregate dashboards alone; inspect raw run provenance and the exact scorer revision.
- Keep random seeds, frozen baselines, run sizes, capability negotiation, and score versions explicit.
- Treat LongMemEval, Hermes, and OpenClaw as adapters using the normal LLM/scoring path, not alternate production scorers.
- Never expose host Docker sockets, cloud credentials, provider keys, or Platform tokens to untrusted harnesses.
