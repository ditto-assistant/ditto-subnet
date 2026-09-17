package gen

import (
	"crypto/sha256"
	"encoding/hex"
	"reflect"
	"testing"
)

func privateArtifactDigest(raw []byte) string {
	h := sha256.Sum256(raw)
	return hex.EncodeToString(h[:])
}

func TestDecodePrivateArtifactPinsBytesAndContract(t *testing.T) {
	profile, _ := ProfileForVersion("small", 13)
	base, err := GenerateDatasetWithSurface(4242, profile, 13, SurfaceOptions{Salt: 91})
	if err != nil {
		t.Fatal(err)
	}
	// Plumbing fixture only, not a qualified private transform.
	base.ToolCases[0].Prompt += "\n"
	raw := mustMarshal(t, base)
	got, err := DecodePrivateArtifact(raw, privateArtifactDigest(raw), 4242, "small")
	if err != nil || string(mustMarshal(t, got)) != string(raw) {
		t.Fatalf("valid pinned artifact rejected: %v", err)
	}
	for _, tc := range []struct {
		name string
		edit func(*DatasetArtifact)
	}{
		{"answer", func(a *DatasetArtifact) { a.MemoryCases[0].ExpectedAnswer = "tampered" }},
		{"case count", func(a *DatasetArtifact) { a.MemoryCases = a.MemoryCases[:len(a.MemoryCases)-1] }},
		{"graph", func(a *DatasetArtifact) { a.MemoryWaves[0].Pairs[0].PairID = "tampered" }},
		{"catalog", func(a *DatasetArtifact) { a.Catalog = nil }},
		{"fixture", func(a *DatasetArtifact) { a.ToolFixtures[0].Needle = "tampered" }},
		{"seed", func(a *DatasetArtifact) { a.Seed++ }},
		{"version", func(a *DatasetArtifact) { a.BenchVersion = 12 }},
		{"public salt", func(a *DatasetArtifact) { a.SurfaceSalt = 0 }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			a, err := GenerateDatasetWithSurface(4242, profile, 13, SurfaceOptions{Salt: 91})
			if err != nil {
				t.Fatal(err)
			}
			a.ToolCases[0].Prompt += "\n"
			tc.edit(&a)
			changed := mustMarshal(t, a)
			got, err := DecodePrivateArtifact(changed, privateArtifactDigest(changed), 4242, "small")
			if err == nil || !reflect.DeepEqual(got, DatasetArtifact{}) {
				t.Fatalf("accepted changed contract: %v", err)
			}
		})
	}
	// Even semantically irrelevant byte edits must disagree with the lease pin.
	if _, err := DecodePrivateArtifact(append(raw, '\n'), privateArtifactDigest(raw), 4242, "small"); err == nil {
		t.Fatal("accepted changed bytes under old digest")
	}
	if _, err := DecodePrivateArtifact(raw, privateArtifactDigest(raw), 4242, "medium"); err == nil {
		t.Fatal("accepted wrong profile")
	}
	if _, err := DecodePrivateArtifact(raw, privateArtifactDigest(raw), 4242, "invalid"); err == nil {
		t.Fatal("accepted unknown profile")
	}
}
