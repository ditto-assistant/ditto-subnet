// Command v5calibrate is the hermetic (no-key, no-Docker) offline calibration for
// the bench_version 5 conversational-sanity gate. It runs several HARNESS
// ARCHETYPES against REAL generated v5 datasets across many rotating seeds,
// scores each through the real scorer.AggregateForVersion(v5) pipeline, and
// reports the per-archetype composite mean and standard error plus the
// conversational_sanity metric.
//
// It is the offline analog of the v5 plan's 4.10 winnability / statistical-power
// precheck and the 4.11 ship gate: it shows, with cross-seed variance, that the
// v5 gate drops a Unicorn-style leak-router and a no-model cleartext parser below
// the 0.85 champion line while an honest harness stays in its fair band.
//
// CALIBRATION TRUST WARNING (identical in spirit to cmd/benchcal): the per-type
// competence numbers below are SYNTHETIC ARCHETYPE PROFILES, not measured from a
// real locked model. They encode the qualitative behavior the v5 plan targets
// (an honest harness grounds conversational turns; a routing exploiter leaks or
// fails to capture declaratives; a cleartext parser cannot grep a coined value or
// a greeting). The ON-MODEL winnability precheck against the frozen-Qwen
// reference (real per-type competence AND run-to-run variance) still requires the
// model relay and a harness image; drive that with cmd/calibrate against a
// running API once the relay is reachable. This pass covers the half that can be
// measured hermetically and committed.
//
//	v5calibrate --seeds 24 --run-size full            # human table
//	v5calibrate --seeds 24 --run-size full --json     # machine-readable
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math"
	"os"

	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Archetype is a named per-question-type competence profile: the probability a
// harness of that kind answers a case of a given type correctly.
type Archetype struct {
	Name        string
	Description string
	// Competence maps question_type -> P(correct); unlisted types fall back to
	// Default.
	Competence map[string]float64
	Default    float64
}

// ordinaryHonest is a mid-tier honest reasoner's competence on the ordinary v4
// memory types (mirrors cmd/benchcal's placeholder shape). The archetypes below
// share it and override only the v5 conversational/declarative types.
var ordinaryHonest = map[string]float64{
	"single-session-recall":  0.90,
	"multi-session":          0.65,
	"temporal-reasoning":     0.50,
	"knowledge-update":       0.72,
	"preference":             0.88,
	"preference-application": 0.62,
	"contradiction":          0.50,
	"abstention":             0.60,
	"aggregation-count":      0.50,
	"computed-answer":        0.42,
	"point-in-time":          0.45,
	"assistant-recall":       0.62,
	"injection-resistance":   0.70,
	"isolation":              0.75,
	"canary":                 0.65,
	"memory-write":           0.90,
	"memory-write-read":      0.80,
}

// ordinaryRetrievalStrong is a strong-retrieval competence on the ordinary types:
// the aligned half of the champion stack (v5 plan section 2) both the routing
// exploiter and a good honest harness ship.
var ordinaryRetrievalStrong = map[string]float64{
	"single-session-recall":  0.95,
	"multi-session":          0.80,
	"temporal-reasoning":     0.60,
	"knowledge-update":       0.85,
	"preference":             0.90,
	"preference-application": 0.75,
	"contradiction":          0.60,
	"abstention":             0.55,
	"aggregation-count":      0.70,
	"computed-answer":        0.60,
	"point-in-time":          0.60,
	"assistant-recall":       0.80,
	"injection-resistance":   0.55,
	"isolation":              0.70,
	"canary":                 0.70,
	"memory-write":           0.90,
	"memory-write-read":      0.85,
}

// ordinaryParser is a no-model cleartext parser: it greps the seeded pairs, so it
// aces the seeded-substring recall types and collapses on the compute-over-many
// types (v5 plan section 1). It cannot grep a coined value that never entered the
// haystack (the declarative writes), which is handled in the archetype override.
var ordinaryParser = map[string]float64{
	"single-session-recall":  0.98,
	"multi-session":          0.90,
	"temporal-reasoning":     0.55,
	"knowledge-update":       0.90,
	"preference":             0.95,
	"preference-application": 0.55,
	"contradiction":          0.60,
	"abstention":             0.40, // a parser tends to surface a candidate rather than decline
	"aggregation-count":      0.45,
	"computed-answer":        0.40,
	"point-in-time":          0.85,
	"assistant-recall":       0.85,
	"injection-resistance":   0.30,
	"isolation":              0.60,
	"canary":                 0.55,
	"memory-write":           0.80,
	"memory-write-read":      0.90,
}

// merge overlays the v5 conversational/declarative overrides onto an ordinary
// profile.
func merge(base map[string]float64, over map[string]float64) map[string]float64 {
	out := make(map[string]float64, len(base)+len(over))
	for k, v := range base {
		out[k] = v
	}
	for k, v := range over {
		out[k] = v
	}
	return out
}

// archetypes are the harnesses run against v5. The v5 conversational/declarative
// overrides are where the plan's thesis lives.
var archetypes = []Archetype{
	{
		Name:        "honest",
		Description: "a grounded reasoning harness: grounds conversational turns and captures plain declaratives",
		Default:     0.55,
		Competence: merge(ordinaryHonest, map[string]float64{
			gen.QTChitchat:            1.00,
			gen.QTDeclarativeAck:      0.97,
			gen.QTAbstainConfab:       0.90,
			gen.QTDeclarativeWrite:    1.00,
			gen.QTDeclarativeRead:     0.90,
			gen.QTDeclarativeBehavior: 0.85,
		}),
	},
	{
		Name:        "honest-strong",
		Description: "the SAME strong retrieval stack as the router, but it grounds conversational turns (the counter-guard: only the conversational half differs)",
		Default:     0.55,
		Competence: merge(ordinaryRetrievalStrong, map[string]float64{
			gen.QTChitchat:            1.00,
			gen.QTDeclarativeAck:      0.95,
			gen.QTAbstainConfab:       0.88,
			gen.QTDeclarativeWrite:    1.00,
			gen.QTDeclarativeRead:     0.88,
			gen.QTDeclarativeBehavior: 0.82,
		}),
	},
	{
		Name:        "unicorn-router",
		Description: "strong retrieval + a hardcoded write-cue router that leaks on greetings and never captures declaratives (the v4 exploiter)",
		Default:     0.55,
		Competence: merge(ordinaryRetrievalStrong, map[string]float64{
			gen.QTChitchat:            0.00, // force-routes a greeting into recall and dumps a sentinel
			gen.QTDeclarativeAck:      0.00, // ignores the statement, does not echo
			gen.QTAbstainConfab:       0.05, // reports the nearest neighbor
			gen.QTDeclarativeWrite:    0.10, // leaks on the write turn too
			gen.QTDeclarativeRead:     0.00, // never captured the no-save-verb write
			gen.QTDeclarativeBehavior: 0.00, // cannot apply what it never stored
		}),
	},
	{
		Name:        "cleartext-parser",
		Description: "a no-model harness that greps the seeded pairs (aces seeded-substring recall, cannot grep a coined value or a greeting)",
		Default:     0.45,
		Competence: merge(ordinaryParser, map[string]float64{
			gen.QTChitchat:            0.00, // nothing to grep; dumps a candidate and leaks
			gen.QTDeclarativeAck:      0.00,
			gen.QTAbstainConfab:       0.10,
			gen.QTDeclarativeWrite:    0.30,
			gen.QTDeclarativeRead:     0.00, // coined value never entered the haystack
			gen.QTDeclarativeBehavior: 0.00,
		}),
	},
}

// hashUnit maps (seed, caseID) deterministically into [0,1). Used as the
// Bernoulli draw for per-case correctness so the whole calibration is a pure
// function of the seed set and the archetype profiles (reproducible, no rand).
func hashUnit(seed int64, caseID string) float64 {
	h := uint64(seed)*0x9E3779B97F4A7C15 + 0x100000001b3
	for i := 0; i < len(caseID); i++ {
		h ^= uint64(caseID[i])
		h *= 0x100000001b3
	}
	h ^= h >> 30
	h *= 0xbf58476d1ce4e5b9
	h ^= h >> 27
	h *= 0x94d049bb133111eb
	h ^= h >> 31
	return float64(h%1_000_000) / 1_000_000.0
}

// gradeArchetype turns a v5 memory suite into per-case scores for an archetype: a
// deterministic Bernoulli draw per case against its type competence. Memory-only,
// so the reported composite is the memory-half composite under the v5 gate — the
// axis the conversational gate acts on.
func gradeArchetype(seed int64, suite gen.MemorySuite, a Archetype) []protocol.CaseScore {
	out := make([]protocol.CaseScore, 0, len(suite.Cases))
	for _, sc := range suite.Cases {
		mc := sc.Case
		p, ok := a.Competence[mc.QuestionType]
		if !ok {
			p = a.Default
		}
		correct := hashUnit(seed, mc.ID+"|"+a.Name) < p
		score := 0.0
		if correct {
			score = 1.0
		}
		out = append(out, protocol.CaseScore{
			CaseID:   mc.ID,
			Category: mc.QuestionType,
			Kind:     protocol.KindMemory,
			Score:    score,
			Correct:  correct,
		})
	}
	return out
}

// Result is one archetype's calibrated distribution across the seed set.
type Result struct {
	Archetype       string  `json:"archetype"`
	Description     string  `json:"description"`
	Seeds           int     `json:"seeds"`
	CompositeMean   float64 `json:"composite_mean"`
	CompositeStdErr float64 `json:"composite_std_err"`
	CompositeMin    float64 `json:"composite_min"`
	CompositeMax    float64 `json:"composite_max"`
	MemoryMean      float64 `json:"memory_mean"`
	ConvSanityMean  float64 `json:"conversational_sanity_mean"`
	// ConvSanitySD is the population standard deviation of the conversational-sanity
	// metric across seeds — the between-seed variance we are minimizing so a grounded
	// harness is not randomly gated. Lower is better.
	ConvSanitySD float64 `json:"conversational_sanity_sd"`
	FracBelow085 float64 `json:"frac_below_0_85"`
}

// Calibrate runs every archetype against v5 datasets for the given seeds and
// returns their calibrated distributions. Exported (via the package) so the test
// asserts the ship-gate bar on the same computation the CLI prints.
func Calibrate(seeds []int64, runSize string) ([]Result, error) {
	prof, ok := gen.ProfileForVersion(runSize, protocol.BenchVersionV5)
	if !ok {
		return nil, fmt.Errorf("unknown run_size %q", runSize)
	}
	results := make([]Result, 0, len(archetypes))
	for _, a := range archetypes {
		composites := make([]float64, 0, len(seeds))
		convVals := make([]float64, 0, len(seeds))
		var memSum, convSum float64
		var convN int
		var below int
		for _, seed := range seeds {
			r, err := gen.NewRNGForVersion(seed, protocol.BenchVersionV5)
			if err != nil {
				return nil, err
			}
			suite, err := gen.GenerateMemorySuiteForVersion(r, seed, prof.Mem, prof.Waves, prof.RawPairsFrac, protocol.BenchVersionV5)
			if err != nil {
				return nil, err
			}
			perCase := gradeArchetype(seed, suite, a)
			report := scorer.AggregateForVersion("cal", perCase, protocol.BenchVersionV5)
			composites = append(composites, report.Composite)
			memSum += report.MemoryMean
			if report.ConversationalSanity != nil {
				convSum += *report.ConversationalSanity
				convVals = append(convVals, *report.ConversationalSanity)
				convN++
			}
			if report.Composite < 0.85 {
				below++
			}
		}
		mean, se, lo, hi := stats(composites)
		conv := 0.0
		if convN > 0 {
			conv = convSum / float64(convN)
		}
		convSD := 0.0
		if len(convVals) > 1 {
			var s float64
			for _, v := range convVals {
				d := v - conv
				s += d * d
			}
			convSD = math.Sqrt(s / float64(len(convVals)))
		}
		results = append(results, Result{
			Archetype:       a.Name,
			Description:     a.Description,
			Seeds:           len(seeds),
			CompositeMean:   round4(mean),
			CompositeStdErr: round4(se),
			CompositeMin:    round4(lo),
			CompositeMax:    round4(hi),
			MemoryMean:      round4(memSum / float64(len(seeds))),
			ConvSanityMean:  round4(conv),
			ConvSanitySD:    round4(convSD),
			FracBelow085:    round4(float64(below) / float64(len(seeds))),
		})
	}
	return results, nil
}

func stats(xs []float64) (mean, stdErr, lo, hi float64) {
	if len(xs) == 0 {
		return 0, 0, 0, 0
	}
	lo, hi = xs[0], xs[0]
	var sum float64
	for _, x := range xs {
		sum += x
		if x < lo {
			lo = x
		}
		if x > hi {
			hi = x
		}
	}
	mean = sum / float64(len(xs))
	if len(xs) < 2 {
		return mean, 0, lo, hi
	}
	var ss float64
	for _, x := range xs {
		d := x - mean
		ss += d * d
	}
	variance := ss / float64(len(xs)-1)
	return mean, math.Sqrt(variance / float64(len(xs))), lo, hi
}

func round4(x float64) float64 { return math.Round(x*1e4) / 1e4 }

func main() {
	var nSeeds int
	var base int64
	var runSize string
	var jsonOut bool
	flag.IntVar(&nSeeds, "seeds", 24, "number of rotating seeds")
	flag.Int64Var(&base, "base-seed", 20260720, "first seed (subsequent seeds increment)")
	flag.StringVar(&runSize, "run-size", "full", "run size (small|medium|full)")
	flag.BoolVar(&jsonOut, "json", false, "emit JSON instead of a table")
	flag.Parse()

	seeds := make([]int64, nSeeds)
	for i := range seeds {
		seeds[i] = base + int64(i)
	}
	results, err := Calibrate(seeds, runSize)
	if err != nil {
		log.Fatalf("calibrate: %v", err)
	}

	if jsonOut {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(map[string]any{
			"bench_version": protocol.BenchVersionV5,
			"run_size":      runSize,
			"seeds":         nSeeds,
			"note":          "SYNTHETIC archetype profiles (no model); on-model frozen-Qwen precheck still pending the relay",
			"results":       results,
		})
		return
	}
	fmt.Printf("bench_version 5 hermetic archetype calibration — run_size=%s, %d seeds (SYNTHETIC profiles)\n\n", runSize, nSeeds)
	fmt.Printf("%-18s  %-8s  %-8s  %-12s  %-8s  %-8s  %-10s\n", "archetype", "comp", "±SE", "[min,max]", "conv", "conv±sd", "<0.85")
	for _, r := range results {
		fmt.Printf("%-18s  %-8.3f  %-8.3f  [%.2f,%.2f]  %-8.3f  %-8.3f  %-10.0f%%\n",
			r.Archetype, r.CompositeMean, r.CompositeStdErr, r.CompositeMin, r.CompositeMax, r.ConvSanityMean, r.ConvSanitySD, r.FracBelow085*100)
	}
	fmt.Println("\nNOTE: on-model winnability precheck vs the frozen-Qwen reference still requires the model relay (cmd/calibrate).")
}
