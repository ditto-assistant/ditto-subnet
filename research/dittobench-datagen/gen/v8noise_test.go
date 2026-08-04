package gen

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV8MemoryWritingNoiseCoversEveryScoredDomain(t *testing.T) {
	const seed int64 = 123456789
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV8)
	r, err := NewRNGForVersion(seed, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	suite, err := GenerateMemorySuiteForVersion(r, seed, prof.Mem, prof.Waves, prof.RawPairsFrac, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	minimum := map[string]int{
		"mixed-story": 20,
		"business":    10,
		"personal":    10,
		"travel":      10,
	}
	for domain, want := range minimum {
		if got := suite.WritingNoiseQuestions[domain]; got < want {
			t.Errorf("V8 noisy %s questions=%d, want at least %d; all=%v", domain, got, want, suite.WritingNoiseQuestions)
		}
	}
	if len(suite.WritingNoisePairs) != 0 {
		t.Fatalf("V8 primary suite unexpectedly seeded duplicate transcript pairs: %v", suite.WritingNoisePairs)
	}
}

func TestV8QuestionProjectionNeverChangesAnswerKeys(t *testing.T) {
	const seed int64 = 982451653
	prof, _ := ProfileForVersion("medium", protocol.BenchVersionV8)
	r, err := NewRNGForVersion(seed, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	suite, err := GenerateMemorySuiteForVersion(r, seed, prof.Mem, prof.Waves, prof.RawPairsFrac, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	before := make([]protocol.MemoryCase, len(suite.Cases))
	for i := range suite.Cases {
		before[i] = suite.Cases[i].Case
	}
	// A second projection with a different seed must still be surface-only.
	applyV8MemoryWritingNoise(seed+1, suite.Cases, suite.Waves)
	for i := range suite.Cases {
		got, want := suite.Cases[i].Case, before[i]
		if got.ExpectedAnswer != want.ExpectedAnswer || got.AnswerKind != want.AnswerKind ||
			!equalStrings(got.AcceptAny, want.AcceptAny) || !equalStrings(got.AnswerItems, want.AnswerItems) ||
			!equalStrings(got.DistractorAnswers, want.DistractorAnswers) || got.ForbiddenAnswer != want.ForbiddenAnswer {
			t.Fatalf("projection changed score material for %s", got.ID)
		}
	}
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
