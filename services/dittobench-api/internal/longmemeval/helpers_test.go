package longmemeval

import (
	"encoding/json"
	"fmt"
	"os"
	"testing"
)

const (
	artifactDigestA         = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	artifactDigestB         = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	receiptDigestA          = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
	receiptDigestB          = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
	fixtureLatencyMS uint64 = 1234
)

func loadTestProfile(t *testing.T) Profile {
	t.Helper()
	raw, err := os.ReadFile("testdata/profile-v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var profile Profile
	if err := json.Unmarshal(raw, &profile); err != nil {
		t.Fatal(err)
	}
	if err := profile.Validate(); err != nil {
		t.Fatalf("fixture profile invalid: %v", err)
	}
	return profile
}

func syntheticCases(perCapability int) []Case {
	cases := make([]Case, 0, perCapability*len(capabilityOrder))
	for index := 0; index < perCapability; index++ {
		extractionType := "single-session-user"
		if index%2 == 1 {
			extractionType = "single-session-assistant"
		}
		cases = append(cases,
			Case{QuestionID: fmt.Sprintf("extract-%02d", index), QuestionType: extractionType},
			Case{QuestionID: fmt.Sprintf("multi-%02d", index), QuestionType: "multi-session"},
			Case{QuestionID: fmt.Sprintf("temporal-%02d", index), QuestionType: "temporal-reasoning"},
			Case{QuestionID: fmt.Sprintf("update-%02d", index), QuestionType: "knowledge-update"},
			Case{QuestionID: fmt.Sprintf("preference-%02d", index), QuestionType: "single-session-preference"},
			// Abstention records intentionally retain varied source types. The
			// suffix must remove them from those source strata.
			Case{
				QuestionID:   fmt.Sprintf("abstain-%02d_abs", index),
				QuestionType: []string{"single-session-user", "multi-session", "knowledge-update"}[index%3],
			},
		)
	}
	return cases
}

func selectFixture(t *testing.T) (Profile, Selection) {
	t.Helper()
	profile := loadTestProfile(t)
	selection, err := Select(profile, syntheticCases(profile.CasesPerCapability+5))
	if err != nil {
		t.Fatal(err)
	}
	return profile, selection
}

func balancedOutcomes(selection Selection, correctPerCapability int) []Outcome {
	counts := make(map[Capability]int)
	outcomes := make([]Outcome, 0, len(selection.Cases))
	for _, item := range selection.Cases {
		outcomes = append(outcomes, Outcome{
			QuestionID: item.QuestionID,
			Correct:    counts[item.Capability] < correctPerCapability,
		})
		counts[item.Capability]++
	}
	return outcomes
}

func providerEvidenceFor(profile Profile) []ProviderEvidence {
	result := make([]ProviderEvidence, 0, len(profile.Providers))
	for index, policy := range profile.Providers {
		receiptDigest := receiptDigestA
		if index == 1 {
			receiptDigest = receiptDigestB
		}
		result = append(result, ProviderEvidence{
			Lane:              policy.Lane,
			CostSource:        AuthoritativeCostV1,
			Currency:          "USD",
			Provider:          policy.Provider,
			ProfileRevision:   policy.ProfileRevision,
			Model:             policy.Model,
			Requests:          2,
			Successes:         2,
			ReceiptedRequests: 2,
			PromptTokens:      100,
			CompletionTokens:  20,
			TotalTokens:       120,
			CostUSDmicros:     500,
			ReceiptSetSHA256:  receiptDigest,
		})
	}
	return result
}

func validEvidence(t *testing.T) (Profile, Selection, Score, Evidence) {
	t.Helper()
	profile, selection := selectFixture(t)
	score, err := Aggregate(selection, balancedOutcomes(selection, profile.CasesPerCapability/2))
	if err != nil {
		t.Fatal(err)
	}
	evidence, err := NewEvidence(profile, selection, artifactDigestA, fixtureLatencyMS, score, providerEvidenceFor(profile))
	if err != nil {
		t.Fatal(err)
	}
	return profile, selection, score, evidence
}

func cloneEvidence(value Evidence) Evidence {
	value.Score.PerCapability = append([]CapabilityScore(nil), value.Score.PerCapability...)
	value.ProviderEvidence = append([]ProviderEvidence(nil), value.ProviderEvidence...)
	return value
}
