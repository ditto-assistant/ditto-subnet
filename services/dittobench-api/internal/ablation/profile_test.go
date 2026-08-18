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

func TestConfirmationBenchVersionSupportedAllowList(t *testing.T) {
	t.Parallel()
	for version, want := range map[int]bool{
		8: false, 9: true, 10: false, 11: false, 12: true, 13: false, 0: false,
	} {
		if got := ConfirmationBenchVersionSupported(version); got != want {
			t.Fatalf("ConfirmationBenchVersionSupported(%d) = %v, want %v", version, got, want)
		}
	}
}

func TestFrozenProfileAcceptsV12AndRejectsUnbuiltVersions(t *testing.T) {
	t.Parallel()
	base := coordinatorConfig(2).FrozenProfile
	v12 := base
	v12.BenchVersion = BenchVersionV12
	v12SHA, err := FrozenProfileSHA256(v12)
	if err != nil {
		t.Fatalf("v12 frozen ablation profile must validate: %v", err)
	}
	v9SHA, err := FrozenProfileSHA256(base)
	if err != nil {
		t.Fatal(err)
	}
	if v12SHA == v9SHA {
		t.Fatal("v12 bench_version must move the frozen ablation profile checksum")
	}
	// v10 and v11 never had a confirmation ablation contract built; they must
	// still fail closed even though the string contract name is unchanged.
	for _, version := range []int{8, 10, 11, 13} {
		unbuilt := base
		unbuilt.BenchVersion = version
		if _, err := FrozenProfileSHA256(unbuilt); err == nil {
			t.Fatalf("frozen ablation profile must reject bench_version %d", version)
		}
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
