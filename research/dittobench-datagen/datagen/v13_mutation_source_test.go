package datagen

import (
	"math/rand"
	"reflect"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func TestV13MutationFollowUpUsesSourceNotGrade(t *testing.T) {
	for seed := int64(1); seed <= 20; seed++ {
		world := universe.GenerateForVersion(seed, 3, 13)
		base := protocol.ToolCase{ID: "mutation"}
		update := v13WorldMemoryUpdate(seed, rand.New(rand.NewSource(seed)), base, world, 0)
		deletion := v13WorldMemoryDelete(seed, base, world, 0)
		wantUpdate := v13FollowUpUpdateRead(seed, "read", update, world, 0)
		wantDelete := v13FollowUpDeleteRead(seed, "read", deletion, world, 0)
		update.ExpectedTools = nil
		update.Prompt = "poisoned old prose"
		deletion.ExpectedTools = nil
		deletion.Prompt = "poisoned old prose"
		if got := v13FollowUpUpdateRead(seed, "read", update, universe.World{}, 0); !reflect.DeepEqual(got, wantUpdate) {
			t.Fatal("update follow-up depends on grading or regenerated world")
		}
		if got := v13FollowUpDeleteRead(seed, "read", deletion, universe.World{}, 0); !reflect.DeepEqual(got, wantDelete) {
			t.Fatal("delete follow-up depends on grading or regenerated world")
		}
		update.MutationSource.Day = "Sunday"
		if got := v13FollowUpUpdateRead(seed, "read", update, world, 0); got.EffectAnswer != "Sunday" {
			t.Fatal("changed state did not reach answer")
		}
		deletion.MutationSource.Email = "changed@example.invalid"
		if got := v13FollowUpDeleteRead(seed, "read", deletion, world, 0); got.EffectAnswer != "changed@example.invalid" {
			t.Fatal("retained contact not authoritative")
		}
	}
}
