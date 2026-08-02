package main

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// pinVersion sets the package-level benchVersion for the duration of a test and
// restores it after. The report-shape/discrimination assertions are calibrated
// against the frozen v2 mix; pinning keeps them stable as CurrentBenchVersion
// advances (the default) and its category set + per-category rates shift.
func pinVersion(t *testing.T, v int) {
	t.Helper()
	prev := benchVersion
	benchVersion = v
	t.Cleanup(func() { benchVersion = prev })
}

func TestCalibrateNoiseFloorZero(t *testing.T) {
	// A deterministic harness on a fixed seed must have zero repeat-seed spread —
	// this is what proves benchcal measures dataset difficulty, not harness noise.
	rep := calibrate(6, 60, 20)
	if rep.NoiseFloorStddev != 0 {
		t.Fatalf("noise floor stddev must be 0 for a deterministic harness, got %v", rep.NoiseFloorStddev)
	}
}

func TestCalibrateReportShape(t *testing.T) {
	pinVersion(t, protocol.BenchVersionV2)
	rep := calibrate(8, 90, 30)
	if rep.Runs != 8 || rep.ToolMean.N != 8 {
		t.Fatalf("unexpected run count: %+v", rep.ToolMean)
	}
	if rep.ToolMean.Mean <= 0 || rep.ToolMean.Mean > 1 {
		t.Fatalf("tool_mean out of range: %v", rep.ToolMean.Mean)
	}
	if rep.MemoryMean.N != 8 || rep.Composite.N != 8 {
		t.Fatalf("memory/composite series missing: mem=%d comp=%d", rep.MemoryMean.N, rep.Composite.N)
	}
	if rep.Composite.Mean <= 0 || rep.Composite.Mean > 1 {
		t.Fatalf("composite out of range: %v", rep.Composite.Mean)
	}
	// Structural memory σ must be small — stratification fixes the type mix.
	if rep.MemoryMean.Stddev > 0.02 {
		t.Fatalf("memory structural σ unexpectedly high: %v", rep.MemoryMean.Stddev)
	}
	if len(rep.PerCategory) < 10 {
		t.Fatalf("expected per-category breakdown, got %d categories", len(rep.PerCategory))
	}
	// Discrimination sanity: the keyword router should ace at least one literal
	// category and be defeated by at least one routing trap. Assert on the SET of
	// traps (not one brittle category): overlap-sensitive traps like
	// route_memory_not_web drift with the RNG draw, but the run-vs-read / edit /
	// missing-arg traps structurally defeat a token-overlap router, so the minimum
	// trap mean stays low regardless of the draw.
	traps := []string{
		"route_memory_not_web", "route_web_not_memory", "agent_run_not_read",
		"agent_read_not_run", "image_edit_not_create", "workflow_not_job", "arg_hallucination",
	}
	minTrap := 1.0
	for _, name := range traps {
		if s, ok := rep.PerCategory[name]; ok && s.Mean < minTrap {
			minTrap = s.Mean
		}
	}
	if minTrap > 0.2 {
		t.Fatalf("expected at least one routing/restraint trap to defeat the keyword router, min trap mean = %.3f", minTrap)
	}
	aced := false
	for _, s := range rep.PerCategory {
		if s.Mean >= 0.8 {
			aced = true
			break
		}
	}
	if !aced {
		t.Fatal("expected the keyword router to ace at least one literal category (mean >= 0.8)")
	}
}

func TestCalibrateDeterministic(t *testing.T) {
	a := calibrate(5, 60, 20)
	b := calibrate(5, 60, 20)
	if a.ToolMean != b.ToolMean {
		t.Fatalf("calibration must be reproducible: %+v vs %+v", a.ToolMean, b.ToolMean)
	}
}

func TestLoadMemProfile(t *testing.T) {
	// Empty path keeps the placeholder (nil, nil).
	if p, err := loadMemProfile(""); p != nil || err != nil {
		t.Fatalf("empty path should yield (nil,nil), got (%v,%v)", p, err)
	}
	dir := t.TempDir()
	good := filepath.Join(dir, "prof.json")
	if err := os.WriteFile(good, []byte(`{"multi-session":0.5,"contradiction":0.3}`), 0o644); err != nil {
		t.Fatal(err)
	}
	p, err := loadMemProfile(good)
	if err != nil {
		t.Fatalf("valid profile should load: %v", err)
	}
	if p["multi-session"] != 0.5 || p["contradiction"] != 0.3 {
		t.Fatalf("profile not loaded correctly: %v", p)
	}
	// Out-of-range value is rejected.
	bad := filepath.Join(dir, "bad.json")
	if err := os.WriteFile(bad, []byte(`{"multi-session":1.5}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := loadMemProfile(bad); err == nil {
		t.Fatal("out-of-range score should be rejected")
	}
	// Empty object is rejected.
	empty := filepath.Join(dir, "empty.json")
	if err := os.WriteFile(empty, []byte(`{}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := loadMemProfile(empty); err == nil {
		t.Fatal("empty profile should be rejected")
	}
}

// A recorded profile that scores every type lower than the placeholder must
// lower memory_mean — proving the seam actually drives scoring. The report also
// records the profile provenance and the configured target sigma.
func TestCalibrateWithMemProfileSeam(t *testing.T) {
	base := calibrate(5, 60, 20)

	lowAll := map[string]float64{
		"single-session-recall":  0.1,
		"multi-session":          0.1,
		"temporal-reasoning":     0.1,
		"knowledge-update":       0.1,
		"preference":             0.1,
		"preference-application": 0.1,
		"contradiction":          0.1,
		"abstention":             0.1,
	}
	// The current benchmark may introduce new memory question families. Build
	// the test profile over the exact five generated suites that calibrateWith
	// will score so an unlisted family cannot silently take the neutral 0.5
	// fallback and make this seam test vacuous.
	for seed := int64(1); seed <= 5; seed++ {
		suite, err := gen.GenerateMemorySuiteForVersion(
			gen.NewRNG(seed), seed, 20, 1, 0, benchVersion,
		)
		if err != nil {
			t.Fatalf("generate memory suite for seed %d: %v", seed, err)
		}
		for _, staged := range suite.Cases {
			lowAll[staged.Case.QuestionType] = 0.1
		}
	}
	cfg := defaultConfig()
	cfg.memProfile = lowAll
	cfg.memProfileSource = "recorded profile: test"
	cfg.targetSigma = 0.02
	got := calibrateWith(5, 60, 20, cfg)

	if got.MemoryMean.Mean >= base.MemoryMean.Mean {
		t.Fatalf("recorded low profile should lower memory_mean: got %v vs placeholder %v",
			got.MemoryMean.Mean, base.MemoryMean.Mean)
	}
	if got.MemProfileSource != "recorded profile: test" {
		t.Fatalf("report should record profile provenance, got %q", got.MemProfileSource)
	}
	if got.TargetSigma != 0.02 {
		t.Fatalf("report should record target sigma, got %v", got.TargetSigma)
	}
	// Tool half is independent of the memory profile.
	if got.ToolMean != base.ToolMean {
		t.Fatalf("tool_mean must not depend on the memory profile: %v vs %v", got.ToolMean, base.ToolMean)
	}
}
