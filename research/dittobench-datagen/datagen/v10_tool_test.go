package datagen

import (
	"math/rand"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV10RoutesSameRequestShapeFromSeededState(t *testing.T) {
	routes := map[string]bool{}
	seen := 0
	for seed := int64(1); seed <= 20; seed++ {
		rotated, err := protocol.RotateSeedForVersion(seed, protocol.BenchVersionV10)
		if err != nil {
			t.Fatal(err)
		}
		cases, _ := GenerateCasesWithFillersForVersion(rand.New(rand.NewSource(rotated)), seed, 100, protocol.BenchVersionV10)
		for _, tc := range cases {
			if tc.Category != "v10_state_dependent_routing" {
				continue
			}
			seen++
			if len(tc.PrerequisitePairs) != 1 {
				t.Fatalf("seed %d case %s has %d route records", seed, tc.ID, len(tc.PrerequisitePairs))
			}
			visible := strings.ToLower(tc.Prompt)
			if strings.Contains(visible, "workflow") || strings.Contains(visible, "ditto code") || strings.Contains(visible, "one-off") {
				t.Fatalf("seed %d prompt reveals the selected route: %q", seed, tc.Prompt)
			}
			var signature []string
			for _, spec := range tc.ExpectedTools {
				signature = append(signature, spec.Name)
				for _, value := range spec.RequiredArgs {
					if strings.Contains(strings.ToLower(tc.Prompt), strings.ToLower(value)) {
						t.Fatalf("seed %d case %s exposes required argument %q verbatim", seed, tc.ID, value)
					}
				}
			}
			routes[strings.Join(signature, " -> ")] = true
		}
	}
	if seen == 0 {
		t.Fatal("v10 did not generate state-dependent routing cases")
	}
	for _, route := range []string{"execute_agent_job", "create_workflow", "list_workflows -> run_workflow"} {
		if !routes[route] {
			t.Errorf("v10 did not exercise route %q across qualification seeds", route)
		}
	}
}
