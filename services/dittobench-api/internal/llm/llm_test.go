package llm

import (
	"crypto/sha256"
	"encoding/hex"
	"testing"
)

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

// TestFrontierOracleProfileRevisionMatchesPlatform re-derives the pinned
// oracle route identity the way Platform's
// inference_routing.aggregate_profile_revision does for a non-gpt-oss model,
// so the two sides cannot drift apart.
func TestFrontierOracleProfileRevisionMatchesPlatform(t *testing.T) {
	canonical := `{"model":"` + FrontierOracleModel +
		`","provider":{"allow_fallbacks":true,"data_collection":"deny","zdr":true}}`
	sum := sha256.Sum256([]byte(canonical))
	want := "openrouter-route-" + hex.EncodeToString(sum[:])[:16] + "-v1"
	if FrontierOracleProfileRevision != want {
		t.Fatalf("FrontierOracleProfileRevision = %q, want %q", FrontierOracleProfileRevision, want)
	}
	if FrontierOracleModel == V7HarnessModel || FrontierOracleProfileRevision == V9AggregateProfileRevision {
		t.Fatal("the oracle route must differ from the bench contract's")
	}
	if !FrontierOracleRoute(FrontierOracleModel, FrontierOracleProfileRevision) ||
		FrontierOracleRoute(FrontierOracleModel, V9AggregateProfileRevision) ||
		FrontierOracleRoute(V7HarnessModel, FrontierOracleProfileRevision) {
		t.Fatal("FrontierOracleRoute must match exactly the pinned pair")
	}
}
