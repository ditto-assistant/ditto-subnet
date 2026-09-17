package datagen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func TestV13ContextDoesNotEncodeDecision(t *testing.T) {
	for seed := int64(1); seed <= 40; seed++ {
		world := universe.GenerateForVersion(seed, 3, protocol.BenchVersionV13)
		for _, family := range []v13RestraintFamily{v13FamilyEffort, v13FamilyCalendar, v13FamilyEmail} {
			for member := 0; member < 3; member++ {
				ask := v13BuildRestraintMember(seed, world, family, 0, member, true, "case")
				act := v13BuildRestraintMember(seed, world, family, 0, member, false, "case")
				if ask.Prompt != act.Prompt {
					t.Fatalf("seed %d family %s: request reveals the decision", seed, family)
				}
				ref := protocol.OpaqueCaseID(seed, "v13-planning-context:"+string(family), member)
				for _, c := range []protocol.ToolCase{ask, act} {
					if !strings.Contains(c.Prompt, ref) || !strings.Contains(pairText(c), ref) {
						t.Fatal("context not shared by request and record")
					}
				}
				if pairText(ask) == pairText(act) || ask.Restraint == nil || len(act.ExpectedTools) == 0 {
					t.Fatal("opposite decisions must be determined by different records")
				}
			}
		}
	}
}
