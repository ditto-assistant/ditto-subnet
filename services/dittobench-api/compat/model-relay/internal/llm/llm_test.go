package llm

import "testing"

// TestHarnessModelFrozen: the locked model id is a code constant, not env-set,
// so HARNESS_MODEL cannot change it. Every validator scores against the same
// model.
func TestHarnessModelFrozen(t *testing.T) {
	t.Setenv("HARNESS_MODEL", "qwen/qwen3-next")
	if got := HarnessModel(); got != LockedHarnessModel {
		t.Fatalf("HARNESS_MODEL must not override the frozen model: got %q want %q", got, LockedHarnessModel)
	}
}

func TestHarnessModelIsVersionBound(t *testing.T) {
	for _, version := range []int{2, 5, 6} {
		if got := HarnessModelForVersion(version); got != LockedHarnessModel {
			t.Fatalf("v%d model = %q, want %q", version, got, LockedHarnessModel)
		}
	}
	if got := HarnessModelForVersion(7); got != V7HarnessModel {
		t.Fatalf("v7 model = %q, want %q", got, V7HarnessModel)
	}
}
