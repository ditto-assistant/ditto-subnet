package gen_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/internal/parserprobe"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// GIH ceilings (issue #1829). They are asserted only against a SURFACE-PASSED
// artifact: on the public pass-off artifact the generator-inverse harness scores
// near the oracle by construction, and that number is the published baseline
// (docs/bench-versions.md). The ceilings become enforceable once the surface-pass
// owner decision (validator commit-reveal salt vs Platform private paraphrase)
// lands; until then the gate below is armed by an environment variable so CI
// keeps measuring the baseline and never blocks on a pass that does not exist.
const (
	// parserprobeStarterKitComposite is the honest starter-kit reference
	// composite the ceiling is defined against; the envelope PR re-pins it from
	// the calibration run.
	parserprobeStarterKitComposite = 0.70
	// parserprobeCeilingMargin is the required gap below the starter kit.
	parserprobeCeilingMargin = 0.05
	// parserprobeSurfaceSlices are the slices the ceiling applies to individually.
	parserprobeBaselineFloor = 0.90
)

var parserprobeSurfaceSlices = []string{"story", "programs", "personal", "quantity", "tool-prompts"}

// parserprobeSurfacePassedEnv names a directory of cmd/generate artifact JSON
// files produced by the salted/private surface pass. When set, the ceiling gate
// runs against them.
const parserprobeSurfacePassedEnv = "DITTOBENCH_SURFACE_PASSED_ARTIFACTS"

// TestParserprobeBaselineOnPublicSeed proves the generator-inverse harness is a
// real adversary: on the pass-off public seed it recovers essentially every
// family and answers near the oracle. This is the published baseline, so a
// generator change that silently lowers it (a frame the parser no longer
// recognises) is caught here rather than mistaken for surface hardening.
func TestParserprobeBaselineOnPublicSeed(t *testing.T) {
	report, err := parserprobe.Run(parserprobe.Options{BenchVersion: protocol.BenchVersionV12, RunSize: "full", FirstSeed: 123456789, Seeds: 1})
	if err != nil {
		t.Fatal(err)
	}
	if len(report.Unclassified) > 0 {
		t.Fatalf("families without a frame bank: %v", report.Unclassified)
	}
	v := report.GIH
	if v.Memory.AnswerRate < 0.99 || v.Memory.Mean < 0.98 {
		t.Errorf("memory answer rate %.4f mean %.4f, want >= 0.99 / 0.98 on the pass-off artifact", v.Memory.AnswerRate, v.Memory.Mean)
	}
	if v.Tool.FamilyIDRate < 0.95 || v.Tool.Mean < 0.95 {
		t.Errorf("tool family-id %.4f mean %.4f, want >= 0.95 (the exported tool banks no longer cover the generator)", v.Tool.FamilyIDRate, v.Tool.Mean)
	}
	if v.Composite < parserprobeBaselineFloor {
		t.Errorf("GIH composite %.4f below the %.2f pass-off baseline floor", v.Composite, parserprobeBaselineFloor)
	}
	for _, slice := range parserprobeSurfaceSlices {
		s, ok := v.Slices[slice]
		if !ok {
			t.Errorf("slice %q missing from the report", slice)
			continue
		}
		t.Logf("pass-off %-13s cases %4d family-id %.4f answer %.4f mean %.4f", slice, s.Cases, s.FamilyIDRate, s.AnswerRate, s.Mean)
	}
	t.Logf("pass-off GIH composite %.4f (memory %.4f, tool %.4f)", v.Composite, v.Memory.Mean, v.Tool.Mean)
}

// TestParserprobeFramesCoverEveryV12Family is the drift guard for the copied
// frame banks: across several seeds every emitted memory question family is
// recognised at a rate that only a generator template change can move.
func TestParserprobeFramesCoverEveryV12Family(t *testing.T) {
	report, err := parserprobe.Run(parserprobe.Options{BenchVersion: protocol.BenchVersionV12, RunSize: "full", FirstSeed: 1, Seeds: 4})
	if err != nil {
		t.Fatal(err)
	}
	for family, s := range report.GIH.Families {
		if family == "no_tool" || family == "abstention" {
			// Both are bare "%s" catch-alls expecting no tool; the wire cannot
			// tell them apart and the outcome is identical, so only the answer counts.
			if s.Mean < 1 {
				t.Errorf("%s: mean %.4f, want 1", family, s.Mean)
			}
			continue
		}
		if strings.HasPrefix(family, "record-balance-cf-") {
			// Base/variant members are byte-identical on the wire to the plain
			// shape; the parser answers them (answer rate) but cannot name the pair.
			if s.AnswerRate < 1 {
				t.Errorf("%s: answer rate %.4f, want 1", family, s.AnswerRate)
			}
			continue
		}
		if s.Cases >= 4 && s.FamilyIDRate < 0.9 {
			t.Errorf("%s: family-id rate %.4f over %d cases, want >= 0.9 (frame bank drifted from the generator)", family, s.FamilyIDRate, s.Cases)
		}
	}
	if len(report.UnmatchedSample) > 0 {
		for _, u := range report.UnmatchedSample {
			t.Logf("unmatched: %s", u)
		}
	}
}

// TestParserprobeRouterVariantRuns proves the N14 seed-trained router is wired:
// trained on a small disjoint seed range it recovers most families and feeds
// the same slot extractors and launder step as the GIH.
func TestParserprobeRouterVariantRuns(t *testing.T) {
	report, err := parserprobe.Run(parserprobe.Options{BenchVersion: protocol.BenchVersionV12, RunSize: "full", FirstSeed: 5, Seeds: 1, RouterSeeds: 6, RouterFirstSeed: 900})
	if err != nil {
		t.Fatal(err)
	}
	if report.Router == nil {
		t.Fatal("router variant missing")
	}
	if report.Router.Memory.FamilyIDRate < 0.8 || report.Router.Tool.FamilyIDRate < 0.8 {
		t.Errorf("router family-id memory %.4f tool %.4f, want >= 0.8 after 6 training seeds", report.Router.Memory.FamilyIDRate, report.Router.Tool.FamilyIDRate)
	}
	t.Logf("router composite %.4f vs GIH %.4f", report.Router.Composite, report.GIH.Composite)
}

// TestParserprobeCeilingOnSurfacePassedArtifacts is the CI ceiling gate. It is
// armed by DITTOBENCH_SURFACE_PASSED_ARTIFACTS (a directory of artifact JSON
// files from the private surface pass) and skipped otherwise, because the
// pass-off artifact scores near the oracle by design.
func TestParserprobeCeilingOnSurfacePassedArtifacts(t *testing.T) {
	dir := os.Getenv(parserprobeSurfacePassedEnv)
	if dir == "" {
		t.Skipf("%s unset: GIH ceilings are report-only until the surface-pass owner decision lands", parserprobeSurfacePassedEnv)
	}
	paths, err := filepath.Glob(filepath.Join(dir, "*.json"))
	if err != nil || len(paths) == 0 {
		t.Fatalf("no artifact JSON under %s", dir)
	}
	var artifacts []gen.DatasetArtifact
	for _, path := range paths {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var a gen.DatasetArtifact
		if err := json.Unmarshal(raw, &a); err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		artifacts = append(artifacts, a)
	}
	report, err := parserprobe.Run(parserprobe.Options{BenchVersion: artifacts[0].BenchVersion, RunSize: "full", Artifacts: artifacts})
	if err != nil {
		t.Fatal(err)
	}
	ceiling := parserprobeStarterKitComposite - parserprobeCeilingMargin
	if report.GIH.Composite > ceiling {
		t.Errorf("GIH composite %.4f exceeds ceiling %.4f on the surface-passed artifacts", report.GIH.Composite, ceiling)
	}
	for _, slice := range parserprobeSurfaceSlices {
		if s := report.GIH.Slices[slice]; s.Mean > ceiling {
			t.Errorf("slice %s: GIH mean %.4f exceeds ceiling %.4f", slice, s.Mean, ceiling)
		}
	}
}
