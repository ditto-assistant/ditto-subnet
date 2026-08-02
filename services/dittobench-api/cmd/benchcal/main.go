// Command benchcal is the offline DittoBench calibration harness.
//
// It measures between-seed variance of the DETERMINISTIC tool suite — the clean
// difficulty signal — by generating N datasets (rotating seed) and scoring each
// against the deterministic reference routing policy (internal/refharness)
// in-process. No Docker, no key: the only thing that varies between runs is the
// dataset, so the spread of tool_mean IS the spread of dataset difficulty. It
// reports overall + per-category mean and stddev, plus a repeat-seed noise
// floor (which must be ~0 for a deterministic harness), as JSON.
//
// The memory-suite / composite σ under a real model needs a harness image;
// drive that with cmd/calibrate --run-size full against a running API. benchcal
// covers the half that can be measured hermetically and committed.
//
//	benchcal --runs 30 --n 90 --out docs/benchcal-toolsuite.json
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math"
	"math/rand"
	"os"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-api/internal/refharness"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// refMemoryCompetence is a FIXED (seed-independent) reference harness profile:
// how well a mid-tier harness answers each v2 memory question type. Because the
// suite stratifies the type mix by a fixed per-run quota, applying a fixed
// per-type score makes the structural memory_mean identical across seeds — i.e.
// v2 removed v1's question-type-draw variance. The residual real σ comes
// from model + harness stochasticity, which this hermetic pass cannot
// see; it is measured by the hosted 30-seed run.
//
// CALIBRATION TRUST WARNING: these numbers are a SYNTHETIC placeholder, not
// measured from a real model. They only fix the type MIX so structural σ is
// isolated; they say NOTHING about a real locked-model harness's per-type
// competence or its run-to-run variance. A zero-variance parser and a real
// reasoning harness that happen to share this mean have very different variance,
// so a sigma gate tuned against these numbers will happily certify a
// deterministic parser as a "stable champion". To recalibrate honestly, record a
// real locked-model harness's per-type competence (mean AND variance) over many
// seeds and feed it in via --mem-profile; do NOT trust the placeholder for any
// gate decision. See docs/calibration-trust.md.
var refMemoryCompetence = map[string]float64{
	"single-session-recall":  0.90,
	"multi-session":          0.60,
	"temporal-reasoning":     0.45,
	"knowledge-update":       0.70,
	"preference":             0.88,
	"preference-application": 0.60,
	"contradiction":          0.45,
	"abstention":             0.55,
}

// memProfileScore looks a question type up in a competence profile, falling back
// to 0.5 for an unlisted type. A nil profile uses the synthetic placeholder.
func memProfileScore(profile map[string]float64, qtype string) float64 {
	if profile == nil {
		profile = refMemoryCompetence
	}
	if v, ok := profile[qtype]; ok {
		return v
	}
	return 0.5
}

// loadMemProfile reads a recorded per-type competence profile from a JSON file
// mapping v2 question type -> mean score in [0,1], e.g.
//
//	{"single-session-recall": 0.82, "multi-session": 0.51, ...}
//
// This is the seam for HONEST calibration: replace the synthetic
// refMemoryCompetence placeholder with numbers measured from a real
// locked-model harness. Values are range-checked; an empty path returns
// (nil, nil) so the caller keeps the placeholder.
func loadMemProfile(path string) (map[string]float64, error) {
	if strings.TrimSpace(path) == "" {
		return nil, nil
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read mem-profile %s: %w", path, err)
	}
	var m map[string]float64
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, fmt.Errorf("parse mem-profile %s: %w", path, err)
	}
	if len(m) == 0 {
		return nil, fmt.Errorf("mem-profile %s is empty", path)
	}
	for k, v := range m {
		if v < 0 || v > 1 {
			return nil, fmt.Errorf("mem-profile %s: score for %q = %v is outside [0,1]", path, k, v)
		}
	}
	return m, nil
}

// Stat summarizes a series of per-seed values.
type Stat struct {
	Mean   float64 `json:"mean"`
	Stddev float64 `json:"stddev"`
	Min    float64 `json:"min"`
	Max    float64 `json:"max"`
	N      int     `json:"n"`
}

// calibConfig bundles the tunable calibration inputs so the gate + difficulty
// targets live in one place, sourced from flags/files rather than hardcoded
// constants. See docs/calibration-trust.md for what each knob means and why the
// defaults must NOT be trusted for a real gate decision.
type calibConfig struct {
	// memProfile is the per-question-type competence policy driving the hermetic
	// memory_mean. nil ⇒ the synthetic refMemoryCompetence placeholder.
	memProfile map[string]float64
	// memProfileSource labels where memProfile came from (for the report).
	memProfileSource string
	// targetSigma is the DESIGN-TARGET composite σ the KOTH margin/score_tol
	// retune assumes. It is recorded in the report and (when maxCompositeStddev
	// gating is on) compared against the measured hermetic σ. It MUST be set from
	// a real locked-model harness's measured variance, not this hermetic pass.
	targetSigma float64
}

// defaultConfig is the placeholder configuration: synthetic memory profile and
// the historical σ ≤ 0.01 design target. Honest calibration overrides both.
func defaultConfig() calibConfig {
	return calibConfig{
		memProfile:       nil,
		memProfileSource: "synthetic placeholder (refMemoryCompetence)",
		targetSigma:      0.01,
	}
}

// Report is the committed calibration artifact.
type Report struct {
	Runs             int             `json:"runs"`
	NTools           int             `json:"n_tools"`
	NMem             int             `json:"n_mem"`
	Harness          string          `json:"harness"`
	MemProfileSource string          `json:"mem_profile_source"` // provenance of the memory competence policy
	TargetSigma      float64         `json:"target_sigma"`       // design-target composite σ the KOTH retune assumes
	ToolMean         Stat            `json:"tool_mean"`
	MemoryMean       Stat            `json:"memory_mean"` // structural (fixed reference policy)
	Composite        Stat            `json:"composite"`   // 0.5 tool + 0.5 memory, per seed
	PerCategory      map[string]Stat `json:"per_category"`
	NoiseFloorStddev float64         `json:"noise_floor_stddev"` // repeat-seed σ; must be ~0
	Note             string          `json:"note"`
}

// benchVersion is the bench contract benchcal generates against, set from the
// --version flag. Default CurrentBenchVersion so a bare run measures the current
// contract (v5, incl. the Code Mode tool categories and the scaled memory suite).
var benchVersion = protocol.CurrentBenchVersion

// runOnce generates the tool dataset for a seed, routes each case through the
// deterministic reference policy, and returns the overall tool_mean plus the
// per-category means for that seed.
func runOnce(seed int64, n int) (float64, map[string]float64) {
	rotated, err := protocol.RotateSeedForVersion(seed, benchVersion)
	if err != nil {
		log.Fatalf("rotate seed: %v", err)
	}
	toolCases, _ := datagen.GenerateCasesWithFillersForVersion(rand.New(rand.NewSource(rotated)), seed, n, benchVersion)
	tools := catalog.Catalog()
	var sum float64
	catSum := map[string]float64{}
	catCount := map[string]int{}
	for _, c := range toolCases {
		resp := protocol.RunResponse{ToolCalls: refharness.Route(c.Prompt, tools), LatencyMs: 1}
		cs := scorer.ScoreToolCase(c, resp, true)
		sum += cs.ToolScore
		catSum[c.Category] += cs.ToolScore
		catCount[c.Category]++
	}
	perCat := map[string]float64{}
	for cat, s := range catSum {
		perCat[cat] = s / float64(catCount[cat])
	}
	mean := 0.0
	if len(toolCases) > 0 {
		mean = sum / float64(len(toolCases))
	}
	return mean, perCat
}

// memRunOnce builds the v2 memory suite for a seed (deterministically) and
// scores it under the fixed reference-competence policy — a hermetic proxy for
// structural memory_mean.
func memRunOnce(seed int64, nMem int, profile map[string]float64) float64 {
	// Calibration measures the CURRENT benchmark, so pin the version explicitly;
	// the un-suffixed wrapper now defaults to the frozen v2 contract.
	suite, err := gen.GenerateMemorySuiteForVersion(gen.NewRNG(seed), seed, nMem, 1, 0, benchVersion)
	if err != nil {
		log.Fatalf("generate memory suite: %v", err)
	}
	if len(suite.Cases) == 0 {
		return 0
	}
	var sum float64
	for _, sc := range suite.Cases {
		sum += memProfileScore(profile, sc.Case.QuestionType)
	}
	return sum / float64(len(suite.Cases))
}

// calibrate runs `runs` seeds with the default (placeholder) configuration.
// Preserved for existing callers/tests; calibrateWith takes an explicit config.
func calibrate(runs, n, nMem int) Report {
	return calibrateWith(runs, n, nMem, defaultConfig())
}

// calibrateWith runs `runs` seeds (1..runs) plus a repeat-seed noise-floor pass,
// scoring memory under cfg.memProfile and recording cfg's provenance/targets.
func calibrateWith(runs, n, nMem int, cfg calibConfig) Report {
	toolMeans := make([]float64, 0, runs)
	memMeans := make([]float64, 0, runs)
	composites := make([]float64, 0, runs)
	catSeries := map[string][]float64{}
	for i := 1; i <= runs; i++ {
		mean, perCat := runOnce(int64(i), n)
		mm := memRunOnce(int64(i), nMem, cfg.memProfile)
		toolMeans = append(toolMeans, mean)
		memMeans = append(memMeans, mm)
		composites = append(composites, 0.5*mean+0.5*mm)
		for cat, v := range perCat {
			catSeries[cat] = append(catSeries[cat], v)
		}
	}

	// Noise floor: the same seed repeated must give identical tool_mean (σ=0),
	// confirming the measurement isolates dataset difficulty, not harness noise.
	var floor []float64
	for r := 0; r < 5; r++ {
		m, _ := runOnce(7, n)
		floor = append(floor, m)
	}

	perCat := map[string]Stat{}
	for cat, series := range catSeries {
		perCat[cat] = stat(series)
	}
	return Report{
		Runs:             runs,
		NTools:           n,
		NMem:             nMem,
		Harness:          "refharness (tools) + fixed reference-competence policy (memory)",
		MemProfileSource: cfg.memProfileSource,
		TargetSigma:      cfg.targetSigma,
		ToolMean:         stat(toolMeans),
		MemoryMean:       stat(memMeans),
		Composite:        stat(composites),
		PerCategory:      perCat,
		NoiseFloorStddev: stat(floor).Stddev,
		Note: "Hermetic structural variance ONLY: tool σ = deterministic routing difficulty; memory σ ≈ 0 shows v2 stratification removed question-type-mix variance. The memory policy is a " + cfg.memProfileSource +
			" — a zero-variance parser, so this pass CANNOT see real champion-region composite σ (judge + harness noise). The KOTH margin/score_tol retune assumes target_sigma and MUST be reconfirmed against a real locked-model harness's measured variance (record it via --mem-profile / the hosted multi-seed run), never against this refharness lookup. See docs/calibration-trust.md.",
	}
}

func main() {
	runs := flag.Int("runs", 30, "number of seeds (1..runs)")
	n := flag.Int("n", 90, "tool cases per run")
	nMem := flag.Int("nmem", 50, "memory cases per run")
	version := flag.Int("version", protocol.CurrentBenchVersion, "bench_version contract to measure")
	out := flag.String("out", "", "write the JSON report to this path (default stdout)")
	memProfile := flag.String("mem-profile", "",
		"path to a JSON per-question-type competence profile RECORDED from a real "+
			"locked-model harness (overrides the synthetic placeholder). See docs/calibration-trust.md.")
	targetSigma := flag.Float64("target-sigma", 0.01,
		"DESIGN-TARGET composite σ the KOTH margin/score_tol retune assumes; MUST come "+
			"from a real locked-model harness's measured variance, not this hermetic pass.")
	maxCompositeStddev := flag.Float64("max-composite-stddev", 0,
		"if > 0, FAIL (exit 1) when the measured hermetic composite σ exceeds this gate. "+
			"WARNING: the hermetic memory policy is zero-variance, so this gate only bounds "+
			"tool-routing difficulty — it does NOT certify real champion-region stability.")
	flag.Parse()

	if !protocol.SupportedBenchVersion(*version) {
		log.Fatalf("unsupported bench_version %d", *version)
	}
	benchVersion = *version

	cfg := defaultConfig()
	cfg.targetSigma = *targetSigma
	prof, err := loadMemProfile(*memProfile)
	if err != nil {
		log.Fatalf("benchcal: %v", err)
	}
	if prof != nil {
		cfg.memProfile = prof
		cfg.memProfileSource = "recorded profile: " + *memProfile
	}

	rep := calibrateWith(*runs, *n, *nMem, cfg)
	b, err := json.MarshalIndent(rep, "", "  ")
	if err != nil {
		log.Fatalf("marshal report: %v", err)
	}
	if *out == "" {
		fmt.Println(string(b))
	} else {
		if err := os.WriteFile(*out, append(b, '\n'), 0o644); err != nil {
			log.Fatalf("write %s: %v", *out, err)
		}
		log.Printf("benchcal: %d seeds × (%d tool, %d mem) → %s (tool σ=%.4f, mem σ=%.4f, composite σ=%.4f, noise floor σ=%.4f, mem_profile=%s)",
			rep.Runs, rep.NTools, rep.NMem, *out, rep.ToolMean.Stddev, rep.MemoryMean.Stddev, rep.Composite.Stddev, rep.NoiseFloorStddev, rep.MemProfileSource)
	}

	// Optional gate. Off by default (max-composite-stddev = 0) so the calibrator
	// stays a pure reporter unless a caller opts in. The gate only bounds the
	// hermetic composite σ, which — with the placeholder memory policy — is
	// dominated by tool-routing difficulty and carries NO real harness noise, so a
	// pass here is necessary but not sufficient for champion-region stability.
	if *maxCompositeStddev > 0 && rep.Composite.Stddev > *maxCompositeStddev {
		log.Fatalf("FAIL: hermetic composite σ %.4f exceeds gate %.4f (this bounds tool-routing difficulty only; reconfirm real σ against a locked-model harness — see docs/calibration-trust.md)",
			rep.Composite.Stddev, *maxCompositeStddev)
	}
}

// stat computes mean/stddev(population)/min/max of a series.
func stat(xs []float64) Stat {
	if len(xs) == 0 {
		return Stat{}
	}
	sort.Float64s(xs)
	var sum float64
	for _, x := range xs {
		sum += x
	}
	mean := sum / float64(len(xs))
	var varSum float64
	for _, x := range xs {
		d := x - mean
		varSum += d * d
	}
	return Stat{
		Mean:   round4(mean),
		Stddev: round4(math.Sqrt(varSum / float64(len(xs)))),
		Min:    round4(xs[0]),
		Max:    round4(xs[len(xs)-1]),
		N:      len(xs),
	}
}

func round4(x float64) float64 { return math.Round(x*1e4) / 1e4 }
