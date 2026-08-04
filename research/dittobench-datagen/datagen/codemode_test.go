package datagen

import (
	"math/rand"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// genToolsAt builds n tool cases at a bench version (mirrors the rng setup the
// artifact path uses).
func genToolsAt(t *testing.T, seed int64, n, version int) []protocol.ToolCase {
	t.Helper()
	rotated, err := protocol.RotateSeedForVersion(seed, version)
	if err != nil {
		t.Fatal(err)
	}
	cases, _ := GenerateCasesWithFillersForVersion(rand.New(rand.NewSource(rotated)), seed, n, version)
	return cases
}

// TestCodeModeIsV5Only pins that the Code Mode categories (run_code compute and
// the run_code-vs-execute_agent_job discrimination, plus search_tools discovery)
// appear ONLY under v5. Earlier contracts must never emit them, so their tool
// bytes stay frozen.
func TestCodeModeIsV5Only(t *testing.T) {
	codeModeTools := map[string]bool{"run_code": true, "search_tools": true}
	codeModeCats := map[string]bool{"code_compute": true, "code_compute_not_agent_job": true, "tool_discovery": true}

	for _, version := range []int{protocol.BenchVersionV2, protocol.BenchVersionV3, protocol.BenchVersionV4} {
		for _, c := range genToolsAt(t, 4242, 200, version) {
			if codeModeCats[c.Category] {
				t.Errorf("v%d emitted a code-mode category %q", version, c.Category)
			}
			for _, ts := range c.ExpectedTools {
				if codeModeTools[ts.Name] {
					t.Errorf("v%d expected a code-mode tool %q", version, ts.Name)
				}
			}
		}
	}

	// v5 must emit all three code-mode categories.
	seen := map[string]bool{}
	for _, c := range genToolsAt(t, 4242, 200, protocol.BenchVersionV5) {
		if codeModeCats[c.Category] {
			seen[c.Category] = true
		}
	}
	for cat := range codeModeCats {
		if !seen[cat] {
			t.Errorf("v5 did not emit code-mode category %q", cat)
		}
	}
}

// TestCodeComputeExpectsRunCode pins the discrimination: a code_compute /
// code_compute_not_agent_job case expects run_code, so a harness that dispatches
// execute_agent_job (the heavy coding harness) for a pure in-context calculation
// is trajectory-wrong.
func TestCodeComputeExpectsRunCode(t *testing.T) {
	n := 0
	for _, c := range genToolsAt(t, 99, 300, protocol.BenchVersionV5) {
		if c.Category != "code_compute" && c.Category != "code_compute_not_agent_job" {
			continue
		}
		n++
		if len(c.ExpectedTools) != 1 || c.ExpectedTools[0].Name != "run_code" {
			t.Errorf("case %q must expect run_code, got %+v", c.Category, c.ExpectedTools)
		}
	}
	if n == 0 {
		t.Fatal("no code_compute cases generated at v5")
	}
}
