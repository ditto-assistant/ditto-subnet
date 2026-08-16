package protocol

import (
	"fmt"
	"time"
)

// Benchmark versions are immutable generation contracts. CurrentBenchVersion is
// used only for display and release metadata; canonical generation must always
// receive an explicit version so historical datasets remain reproducible after
// a release advances.
//
// v2 is frozen: it is what on-chain scoring has been running since 2026-07-13,
// and its bytes must keep regenerating identically so any already-scored run
// stays auditable. v3 is the ANTI-GAMING release -- dump-guard grading, needle
// gating, adversarial distractors, composed injection framings, the cross-user
// lifecycle probe, and the reproduce-under-transform audit. Every one of those
// is gated on the version, so adding hardening never disturbs v2's bytes.
// v4 is the FALSE-POSITIVE release. v3's anti-gaming machinery penalized a
// number of legitimate behaviours: the canary could be audit-duplicated so one
// breach was charged twice, delete INSTRUCTIONS were graded on echoing a noun
// phrase, and several composite factors taxed a transparent memory agent for how
// it retrieves. Those are corrected here rather than in v3, because a bench
// version is an immutable contract: v3's bytes and its already-recorded scores
// stay exactly as they were, and re-scoring happens by moving to a new contract
// rather than by rewriting an old one.
// v5 is the CONVERSATIONAL-GROUNDING, COVERAGE, AND EFFICIENCY release (see
// dittobench-api docs/BENCHMARK-V5-PLAN.md). v4 rewarded a phrase-list router that
// recognizes memory writes only through a closed cue family and dumps retrieved
// memory on any unmatched turn (the "Aurora-9" failure): a greeting could surface
// an off-topic seeded value and still score near the top, because no case graded
// conversational relevance and no case tested a plain declarative statement as a
// durable write. v5 adds, all version-gated so v4's bytes and already-recorded
// scores are untouched:
//   - a conversational-sanity gate (greeting non-leak, declarative acknowledgement,
//     abstention-over-confabulation) and ordinary no-save-verb declarative writes
//     with their persistence and behavior-change proofs;
//   - the accept-set grading primitive (non-verbatim answers), Code Mode tool
//     coverage, and harder capability dimensions (multi-hop relational KG joins,
//     temporal-depth), all high-entropy and metamorphic-twinned to resist overfit;
//   - a token-efficiency (waste-penalty) contract the validator applies from
//     trusted relay telemetry, binding generation, model/provider profile, and the
//     starter baseline to one immutable contract.
//
// v6 is the MEMORY-AS-DATA release: stored-instruction injection, multi-query
// fan-out, non-verbatim computed answers, and passive cross-session
// consolidation, all version-gated so v5's bytes are untouched.
// v7 carries the OpenRouter/gpt-oss-20b inference boundary AND is the DIFFICULTY
// release (see docs/bench-versions.md and docs/v7-product-traceability.md). Every
// new case family mirrors a real Ditto product flow, data-model shape, or
// documented failure mode (deep write chains, three-hop KG joins, near-miss
// entity-disambiguation abstention, temporal aggregation, composed
// stored-instruction injection, subscribed-graph attribution; plus dependent
// tool chains). Every lever is gated on bench_version >= 7, so v2..v6 regenerate
// and grade byte-identically. The judge-free deterministic grader is unchanged,
// and the wire/artifact format stays backward-compatible (additive only). The
// difficulty is defined and measured, not asserted. The champion tier is REFIT
// to the leaderboard rebench (five harnesses measured 0.59-0.795 on the
// round-1-deepened suite); the round-3 refitted sim predicts scratch/starter
// case-means of 0.758/0.669 (composite ~0.57-0.66 after the observed robustness
// gate), the best non-reasoning strategy scores ~0.09 on the reasoning subset,
// and a correct oracle scores 1.0 on every case across seeds
// (gen.TestV7ChampionTierRefit / TestV7NaiveStrategiesCollapse /
// TestV7OracleSolvable). See docs/v7-product-traceability.md.
//
// v8 is the ANSWERING-MACHINE-PROOF difficulty release. It keeps v7's run sizes,
// inference model, and runtime envelope, but makes the seeded tool mix vary,
// makes natural follow-up requests depend on seed-bound prior context, keeps
// exact argument checks for product enums/identifiers while accepting arbitrary
// valid free-form wording, raises the computed-memory floor, and tightens the
// deterministic grader. Every lever is gated on bench_version >= 8 so v7 remains
// independently reproducible.
//
// v9 is generated explicitly for pre-activation qualification. It inherits the
// v8 product-semantic world and adds a final scored-family floor whose residual
// mix varies by seed. CurrentBenchVersion deliberately remains v8: supporting
// deterministic v9 generation is not the same as advertising or activating it.
//
// v10 is the generator-as-specification contract. It adds seed-scoped ontology
// and schema labels, recursive query programs, independent renderers, and
// first-class metamorphic/counterfactual provenance. It is generation-only
// until the private qualification stack proves durable headroom.
//
// v11 is the ANTI-TEMPLATE-FITTING release, designed against the two harness
// archetypes measured at the top of the v10 board: deterministic rule engines
// that (a) prefix-match the generator's fixed surface templates, (b) hardcode
// the single v10 balance program, (c) resolve entities only through literal
// alias echo, and (d) assume the v9 single-edit noise projector. All levers are
// gated on bench_version >= 11 so v10 regenerates byte-identically:
//   - varied recursive program shapes per metamorphic group (subtract,
//     adjust-then-subtract, latest-of-two-corrections, larger-minus-settled)
//     instead of one fixed balance formula;
//   - compositional surface grammar for glossaries, questions, and assistant
//     acknowledgements (seeded recombination, no stable literal prefixes);
//   - descriptive entity binding: half the groups reference the subject by a
//     unique evidence-derived description rather than by its alias;
//   - a multi-edit surface-noise projector applied to non-critical tokens of
//     seeded history and questions (answers and schema labels stay verbatim);
//   - per-seed composed stored-instruction injection markers instead of the
//     fixed marker list.
//
// The deterministic grader, run sizes, inference boundary, LongMemEval
// deep-history floors, and the v9 signed-evidence/score-gate/curve-v3
// efficiency stack all carry forward unchanged.
const (
	BenchVersionV2      = 2
	BenchVersionV3      = 3
	BenchVersionV4      = 4
	BenchVersionV5      = 5
	BenchVersionV6      = 6
	BenchVersionV7      = 7
	BenchVersionV8      = 8
	BenchVersionV9      = 9
	BenchVersionV10     = 10
	BenchVersionV11     = 11
	CurrentBenchVersion = BenchVersionV8

	// BenchVersion is retained as a source-compatible alias for consumers that
	// report the active release. Generation code must not use it to select a
	// dataset contract.
	BenchVersion = CurrentBenchVersion
)

var (
	datasetEpochV2  = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	datasetEpochV3  = time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC)
	datasetEpochV4  = time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC)
	datasetEpochV5  = time.Date(2026, 9, 1, 0, 0, 0, 0, time.UTC)
	datasetEpochV6  = time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	datasetEpochV7  = time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)
	datasetEpochV8  = time.Date(2026, 12, 1, 0, 0, 0, 0, time.UTC)
	datasetEpochV9  = time.Date(2027, 1, 1, 0, 0, 0, 0, time.UTC)
	datasetEpochV10 = time.Date(2027, 2, 1, 0, 0, 0, 0, time.UTC)
	datasetEpochV11 = time.Date(2027, 3, 1, 0, 0, 0, 0, time.UTC)

	// DatasetEpoch and DatasetEpochRFC3339 retain the v2 values for legacy
	// package callers. Canonical versioned generation uses DatasetEpochForVersion.
	DatasetEpoch        = datasetEpochV2
	DatasetEpochRFC3339 = datasetEpochV2.Format(time.RFC3339)
)

// SupportedBenchVersion reports whether this module can reproduce a version.
func SupportedBenchVersion(version int) bool {
	return version == BenchVersionV2 || version == BenchVersionV3 ||
		version == BenchVersionV4 || version == BenchVersionV5 ||
		version == BenchVersionV6 || version == BenchVersionV7 ||
		version == BenchVersionV8 || version == BenchVersionV9 ||
		version == BenchVersionV10 || version == BenchVersionV11
}

// DatasetEpochForVersion returns the immutable reference instant for version.
func DatasetEpochForVersion(version int) (time.Time, error) {
	switch version {
	case BenchVersionV2:
		return datasetEpochV2, nil
	case BenchVersionV3:
		return datasetEpochV3, nil
	case BenchVersionV4:
		return datasetEpochV4, nil
	case BenchVersionV5:
		return datasetEpochV5, nil
	case BenchVersionV6:
		return datasetEpochV6, nil
	case BenchVersionV7:
		return datasetEpochV7, nil
	case BenchVersionV8:
		return datasetEpochV8, nil
	case BenchVersionV9:
		return datasetEpochV9, nil
	case BenchVersionV10:
		return datasetEpochV10, nil
	case BenchVersionV11:
		return datasetEpochV11, nil
	default:
		return time.Time{}, fmt.Errorf("unsupported bench_version %d (supported: 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)", version)
	}
}

// RotateSeedForVersion folds an explicit benchmark version into a run seed.
// It is deterministic and retains the exact historical v2 mixing function.
func RotateSeedForVersion(seed int64, version int) (int64, error) {
	if !SupportedBenchVersion(version) {
		return 0, fmt.Errorf("unsupported bench_version %d (supported: 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)", version)
	}
	v := uint64(version)
	x := uint64(seed) ^ (v * 0x9E3779B97F4A7C15)
	x ^= x >> 30
	x *= 0xBF58476D1CE4E5B9
	x ^= x >> 27
	x *= 0x94D049BB133111EB
	x ^= x >> 31
	return int64(x), nil
}

// RotateSeed retains the v2 behavior for legacy package callers. New canonical
// generation paths must call RotateSeedForVersion with an explicit version.
func RotateSeed(seed int64) int64 {
	rotated, _ := RotateSeedForVersion(seed, BenchVersionV2)
	return rotated
}
