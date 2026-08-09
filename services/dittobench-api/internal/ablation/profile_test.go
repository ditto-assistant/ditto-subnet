package ablation

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestFrozenProfileWireContractIsArtifactIndependent(t *testing.T) {
	t.Parallel()
	config := coordinatorConfig(2)
	body, err := json.Marshal(config.FrozenProfile)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(body, []byte("artifact")) || bytes.Contains(body, []byte(testArtifactSHA)) {
		t.Fatalf("runtime artifact leaked into the frozen profile: %s", body)
	}
	if !bytes.Contains(body, []byte(`"dataset_sha256"`)) || !bytes.Contains(body, []byte(`"coordinator_policy"`)) {
		t.Fatalf("frozen policy bindings are missing: %s", body)
	}
}

func TestFrozenProfileChecksumMovesForEveryFrozenDigestAndPolicy(t *testing.T) {
	t.Parallel()
	base := coordinatorConfig(2).FrozenProfile
	want, err := FrozenProfileSHA256(base)
	if err != nil {
		t.Fatal(err)
	}
	tests := map[string]func(*FrozenProfile){
		"revision":           func(value *FrozenProfile) { value.Revision += "-changed" },
		"dataset":            func(value *FrozenProfile) { value.DatasetSHA256 = strings.Repeat("a", 64) },
		"threshold manifest": func(value *FrozenProfile) { value.ThresholdManifestSHA256 = strings.Repeat("b", 64) },
		"selection key":      func(value *FrozenProfile) { value.SelectionKeySHA256 = strings.Repeat("c", 64) },
		"projection key":     func(value *FrozenProfile) { value.ProjectionKeySHA256 = strings.Repeat("d", 64) },
		"coordinator policy": func(value *FrozenProfile) { value.CoordinatorPolicy.RequestTimeoutMilliseconds++ },
		"inference budget":   func(value *FrozenProfile) { value.InferenceBudget.MaxChatInputBytes++ },
		"embedding budget":   func(value *FrozenProfile) { value.EmbeddingBudget.MaxEmbeddingInputBytes++ },
		"inference threshold": func(value *FrozenProfile) {
			value.InferenceThreshold += 0.01
		},
		"embedding threshold": func(value *FrozenProfile) {
			value.EmbeddingThreshold += 0.01
		},
	}
	for name, mutate := range tests {
		name, mutate := name, mutate
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			changed := base
			mutate(&changed)
			got, err := FrozenProfileSHA256(changed)
			if err != nil {
				t.Fatal(err)
			}
			if got == want {
				t.Fatal("changed frozen field did not move the profile checksum")
			}
		})
	}
}
