# Bench v9 scored-family mix

Bench v9 fixes the dead-family failure recorded in
`ditto-assistant/ditto-subnet#499` without changing any v2-v8 bytes or activating
v9. `protocol.CurrentBenchVersion` remains v8. V9 has separately pinned small,
medium, and full profiles; their counts deliberately equal v8 for cost
continuity, but future versions fail closed until they define their own profile.

## Full-profile contract

The tool selector uses a domain-separated deterministic stream keyed by
`(seed, bench_version, tool_count)`. A full run reserves one case per source
family and samples the remaining 47 source slots from the published v7 weights.
A partial catalog is sampled without replacement by weight. `set_effort`, the
only pure-intent argument family, is mandatory in every non-empty public
profile.

The coherent-world pass then preserves one representative of every non-retired
source family and converts only duplicates. Seven obsolete source families are
retired into production-semantic world families:

| Retired source | V9 scored family |
| --- | --- |
| `email_send` | `world_contact_research_email_result_usage` |
| `memory_delete` | `world_memory_delete` |
| `memory_update` | `world_memory_update` |
| `link_read` | `world_link_chain_result_usage` |
| `multi_job_status`, `job_chain_result_usage`, `job_chain_recovery_result_usage` | `world_agent_job_dispatch` |

A full 100-case artifact has 46 retained non-world families and seven world
families. A one-per-family floor therefore consumes 53 slots and places a hard
mathematical ceiling of 54 world cases on the run. V8's 65% world-conversion
target cannot coexist with this floor.

V9 makes that tradeoff explicit:

- every one of the 53 final tool families appears at least once;
- pure coherent-world actions target 48 cases and have a hard minimum of 43;
- `world_*`, `stale_context_web`, and `memory_fetch` together form the narrowly
  defined evidence-bound/composed envelope, with a hard minimum of 50 cases;
- six to eleven slots beyond the 46 non-world floors retain the seed-sampled
  source weights instead of being silently consumed by world conversion.

Ordinary duplicates are converted first. Only if an unusually large weighted
draw of planted-context cases would miss the 43-case pure-world floor may a
duplicate planted-context case be converted; its sole family representative is
still protected. World-family assignment uses an independent deterministic
stream: the first eight conversions permute eight equal slots, and subsequent
conversions draw uniformly from those slots. Contact/research/email occupies
two slots, so its declared weight is 2 while each other world action has weight
1. It is not a uniform draw over the seven final world-family names.

Output ordering and hostile-harness projection use separate randomness and
cannot perturb the scored histogram.

## Memory audit

The starvation-shaped `weightedTypeQuota` helper is a v2-v7 path. V8 and v9
return through `generateV8WorldMemorySuite` before reaching it. A full v9 run has
22 final memory families. Qualification requires every family, including all
computed project, trip, and story programs, in every run while also requiring
the histogram to vary between seeds.

## Reproducible structural audit

Run from `research/dittobench-datagen`:

```bash
go run ./cmd/vstudy \
  -bench-versions 8,9 \
  -run-size full \
  -seeds 40 > summary40.json

go run ./cmd/vstudy \
  -bench-versions 9 \
  -run-size full \
  -seeds 300 > summary300.json
```

Seeds 1 through 40 produced:

| Measure | V8 | V9 |
| --- | ---: | ---: |
| Distinct tool histograms | 40 | 40 |
| Tool families across sweep | 53 | 53 |
| Minimum final full-run tool-family count | 0 | 1 |
| Runs containing `set_effort` | 12/40 | 40/40 |
| Distinct memory histograms | 40 | 40 |
| Memory families across sweep | 22 | 22 |
| Minimum final full-run memory-family count | 1 | 1 |
| Pure world cases, mean (range) | not a v8 contract | 47.95 (47-48) |
| Evidence-bound/composed cases, mean (range) | not a v8 contract | 53.825 (51-56) |

The final v9 generator also completed full-profile seeds 1 through 300:

- all 53 tool and all 22 memory families had a minimum count of one;
- `set_effort` appeared exactly once in all 300 runs;
- tool histograms were distinct in 300/300 runs and memory histograms in
  299/300;
- pure world cases had mean `47.7967`, population variance `0.4020`, and range
  `43-48`;
- evidence-bound/composed cases had mean `53.9133`, population variance
  `2.6725`, and range `50-56`;
- all seven world families and all 46 retained non-world families varied in
  count somewhere across the sweep; the higher-weight `stale_context_web`
  family had mean `4.6633`, versus `1.4533` for weight-1 `memory_fetch`.

The sweep also found a rare inherited coherent-world isolation collision at
seed 143: projecting the shared given name could make a secondary person's
`(name, employer, event)` tuple non-unique. V9 deterministically adds
source-derived middle-name material in that case and has a dedicated 300-seed
oracle-validity regression. The frozen v8 path remains byte-identical.

## Score calibration is pending

This study publishes structural generation evidence only. The old simulator
assigned unlisted tool families optimistic fallback rates, so it was not valid
boundary or activation evidence for v9. `cmd/vstudy` now disables v9 strategy
simulation and does not emit v9 G-study JSONL. It reports `score_calibration:
pending` until every emitted v9 family has an explicit measured rate and
provenance. No champion composite, score variance, or activation conclusion is
claimed here.

## Required validation

```bash
go test ./...
go test -race ./datagen ./gen ./cmd/vstudy
go vet ./...
go test ./gen -run 'TestV[2-8]KnownVector|TestV9KnownVector|TestSameSeedSameBytes' -count=1
```
