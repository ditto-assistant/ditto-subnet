package gen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestGenerateDatasetReviewKeepsAnnotationsOutOfCanonicalBytes(t *testing.T) {
	const seed int64 = 123456789
	prof, ok := ProfileForVersion("medium", protocol.BenchVersionV8)
	if !ok {
		t.Fatal("missing medium V8 profile")
	}
	review, err := GenerateDatasetReview(seed, prof, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	canonical, err := GenerateDataset(seed, prof, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	reviewSHA, _, err := review.Artifact.SHA256Hex()
	if err != nil {
		t.Fatal(err)
	}
	canonicalSHA, _, err := canonical.SHA256Hex()
	if err != nil {
		t.Fatal(err)
	}
	if reviewSHA != canonicalSHA {
		t.Fatalf("review changed canonical bytes: %s != %s", reviewSHA, canonicalSHA)
	}
	if len(review.MemoryAnnotations) == 0 {
		t.Fatal("V8 review has no memory annotations")
	}

	pairs := map[string]bool{}
	for _, wave := range review.Artifact.MemoryWaves {
		for _, pair := range wave.Pairs {
			pairs[pair.PairID] = true
		}
	}
	for _, toolCase := range review.Artifact.ToolCases {
		for _, pair := range toolCase.PrerequisitePairs {
			pairs[pair.PairID] = true
		}
	}
	cases := map[string]bool{}
	for _, c := range review.Artifact.MemoryCases {
		cases[c.ID] = true
	}
	for _, annotation := range review.MemoryAnnotations {
		if !cases[annotation.CaseID] {
			t.Errorf("annotation references absent case %s", annotation.CaseID)
		}
		if len(annotation.RequiredPairIDs) < 3 {
			t.Errorf("case %s has only %d declared evidence records", annotation.CaseID, len(annotation.RequiredPairIDs))
		}
		if len(annotation.Citations) != len(annotation.RequiredPairIDs) {
			t.Errorf("case %s citations=%d evidence=%d", annotation.CaseID, len(annotation.Citations), len(annotation.RequiredPairIDs))
		}
		for i, citation := range annotation.Citations {
			if citation.PairID != annotation.RequiredPairIDs[i] || citation.SessionID == "" || citation.Timestamp == "" {
				t.Errorf("case %s has incomplete citation %+v", annotation.CaseID, citation)
			}
			if citation.Story != nil {
				if len(citation.Story.Characters) < 2 || len(citation.Story.Problems) < 2 || len(citation.Story.Problems) != len(citation.Story.Resolutions) || len(citation.Story.FactPlacements) < 2 {
					t.Errorf("case %s has incomplete story citation %+v", annotation.CaseID, citation.Story)
				}
			}
		}
		for _, pairID := range annotation.RequiredPairIDs {
			if !pairs[pairID] {
				t.Errorf("case %s evidence %s is absent from artifact", annotation.CaseID, pairID)
			}
		}
	}
	worldCases := 0
	for _, c := range review.Artifact.MemoryCases {
		if strings.HasPrefix(c.QuestionType, "world-") {
			worldCases++
		}
	}
	if len(review.MemoryAnnotations) != worldCases {
		t.Fatalf("review annotations=%d world cases=%d", len(review.MemoryAnnotations), worldCases)
	}
}

func TestGenerateDatasetReviewLeavesV7Unannotated(t *testing.T) {
	prof, ok := ProfileForVersion("small", protocol.BenchVersionV7)
	if !ok {
		t.Fatal("missing small V7 profile")
	}
	review, err := GenerateDatasetReview(42, prof, protocol.BenchVersionV7)
	if err != nil {
		t.Fatal(err)
	}
	if len(review.MemoryAnnotations) != 0 {
		t.Fatalf("V7 review has %d V8 annotations", len(review.MemoryAnnotations))
	}
}
