package longmemeval

import (
	"bytes"
	"encoding/json"
	"math"
	"reflect"
	"strings"
	"testing"
)

func TestProviderEvidenceAcceptsExactBudgetBoundary(t *testing.T) {
	profile := loadTestProfile(t)
	policy := profile.Providers[0]
	evidence := providerEvidenceFor(profile)[0]
	evidence.Requests = policy.MaxRequests
	evidence.Successes = policy.MaxRequests
	evidence.ReceiptedRequests = policy.MaxRequests
	evidence.PromptTokens = policy.MaxPromptTokens
	evidence.CompletionTokens = policy.MaxCompletionTokens
	evidence.TotalTokens = evidence.PromptTokens + evidence.CompletionTokens
	if evidence.TotalTokens > policy.MaxTotalTokens {
		// The fixture intentionally leaves total headroom, but keep the boundary
		// test valid if profile calibration later makes the component sum larger.
		evidence.CompletionTokens = policy.MaxTotalTokens - evidence.PromptTokens
		evidence.TotalTokens = policy.MaxTotalTokens
	}
	evidence.CostUSDmicros = policy.MaxCostUSDmicros
	if err := ValidateProviderEvidence(policy, evidence); err != nil {
		t.Fatalf("exact boundary rejected: %v", err)
	}
}

func TestProviderEvidenceAllowsAuthoritativeZeroCost(t *testing.T) {
	profile := loadTestProfile(t)
	policy := profile.Providers[0]
	evidence := providerEvidenceFor(profile)[0]
	evidence.CostUSDmicros = 0
	if err := ValidateProviderEvidence(policy, evidence); err != nil {
		t.Fatalf("receipted zero cost rejected: %v", err)
	}
}

func TestProviderEvidenceRejectsIdentityDriftAndFallback(t *testing.T) {
	profile := loadTestProfile(t)
	policy := profile.Providers[0]
	base := providerEvidenceFor(profile)[0]
	tests := map[string]func(*ProviderEvidence){
		"lane":             func(e *ProviderEvidence) { e.Lane += "-fallback" },
		"provider":         func(e *ProviderEvidence) { e.Provider += "-fallback" },
		"profile revision": func(e *ProviderEvidence) { e.ProfileRevision += "-fallback" },
		"model":            func(e *ProviderEvidence) { e.Model += "-fallback" },
		"fallback flag":    func(e *ProviderEvidence) { e.FallbackUsed = true },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			changed := base
			mutate(&changed)
			if err := ValidateProviderEvidence(policy, changed); err == nil {
				t.Fatal("provider drift accepted")
			}
		})
	}
}

func TestProviderEvidenceRejectsNonAuthoritativeOrMalformedAccounting(t *testing.T) {
	profile := loadTestProfile(t)
	policy := profile.Providers[0]
	base := providerEvidenceFor(profile)[0]
	tests := map[string]func(*ProviderEvidence){
		"estimated cost":      func(e *ProviderEvidence) { e.CostSource = "model_list_price" },
		"wrong currency":      func(e *ProviderEvidence) { e.Currency = "EUR" },
		"bad receipt digest":  func(e *ProviderEvidence) { e.ReceiptSetSHA256 = "bad" },
		"uppercase receipt":   func(e *ProviderEvidence) { e.ReceiptSetSHA256 = strings.ToUpper(e.ReceiptSetSHA256) },
		"zero requests":       func(e *ProviderEvidence) { e.Requests = 0 },
		"zero successes":      func(e *ProviderEvidence) { e.Successes = 0 },
		"too many successes":  func(e *ProviderEvidence) { e.Successes = e.Requests + 1 },
		"missing receipts":    func(e *ProviderEvidence) { e.ReceiptedRequests-- },
		"inconsistent tokens": func(e *ProviderEvidence) { e.TotalTokens++ },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			changed := base
			mutate(&changed)
			if err := ValidateProviderEvidence(policy, changed); err == nil {
				t.Fatal("malformed accounting accepted")
			}
		})
	}
}

func TestProviderEvidenceRejectsEveryBudgetOverrun(t *testing.T) {
	profile := loadTestProfile(t)
	policy := profile.Providers[0]
	base := providerEvidenceFor(profile)[0]
	tests := map[string]func(*ProviderEvidence){
		"requests": func(e *ProviderEvidence) {
			e.Requests = policy.MaxRequests + 1
			e.Successes = e.Requests
			e.ReceiptedRequests = e.Requests
		},
		"prompt tokens": func(e *ProviderEvidence) {
			e.PromptTokens = policy.MaxPromptTokens + 1
			e.TotalTokens = e.PromptTokens + e.CompletionTokens
		},
		"completion tokens": func(e *ProviderEvidence) {
			e.CompletionTokens = policy.MaxCompletionTokens + 1
			e.TotalTokens = e.PromptTokens + e.CompletionTokens
		},
		"total tokens": func(e *ProviderEvidence) {
			e.PromptTokens = policy.MaxTotalTokens
			e.CompletionTokens = 1
			e.TotalTokens = policy.MaxTotalTokens + 1
		},
		"cost": func(e *ProviderEvidence) { e.CostUSDmicros = policy.MaxCostUSDmicros + 1 },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			changed := base
			mutate(&changed)
			if err := ValidateProviderEvidence(policy, changed); err == nil {
				t.Fatal("budget overrun accepted")
			}
		})
	}
}

func TestProviderEvidenceRejectsTokenAdditionOverflow(t *testing.T) {
	profile := loadTestProfile(t)
	policy := profile.Providers[0]
	policy.MaxPromptTokens = ^uint64(0)
	policy.MaxCompletionTokens = ^uint64(0)
	policy.MaxTotalTokens = ^uint64(0)
	if err := policy.validate(); err == nil {
		t.Fatal("overflowing policy token caps accepted")
	}

	policy = profile.Providers[0]
	evidence := providerEvidenceFor(profile)[0]
	evidence.PromptTokens = ^uint64(0)
	evidence.CompletionTokens = 1
	evidence.TotalTokens = 0 // the value uint64 overflow would otherwise produce
	if err := ValidateProviderEvidence(policy, evidence); err == nil {
		t.Fatal("overflowing observed token sum accepted")
	}
}

func TestNewEvidenceNormalizesProviderOrderAndBindsArtifactDigest(t *testing.T) {
	profile, selection := selectFixture(t)
	score, err := Aggregate(selection, balancedOutcomes(selection, profile.CasesPerCapability/2))
	if err != nil {
		t.Fatal(err)
	}
	providers := providerEvidenceFor(profile)
	providers[0], providers[1] = providers[1], providers[0]
	evidence, err := NewEvidence(profile, selection, artifactDigestA, fixtureLatencyMS, score, providers)
	if err != nil {
		t.Fatal(err)
	}
	if evidence.ProviderEvidence[0].Lane != "judge" || evidence.ProviderEvidence[1].Lane != "reader" {
		t.Fatalf("provider order not canonical: %#v", evidence.ProviderEvidence)
	}
	if evidence.ArtifactSHA256 != artifactDigestA || evidence.CaseSetDigest != selection.CaseSetDigest {
		t.Fatal("evidence did not bind artifact and case-set identities")
	}
}

func TestNewEvidenceRejectsProviderCoverageErrors(t *testing.T) {
	profile, selection := selectFixture(t)
	score, err := Aggregate(selection, balancedOutcomes(selection, profile.CasesPerCapability/2))
	if err != nil {
		t.Fatal(err)
	}
	valid := providerEvidenceFor(profile)
	tests := map[string][]ProviderEvidence{
		"missing lane":   valid[:1],
		"duplicate lane": {valid[0], valid[0]},
		"unexpected lane": {valid[0], func() ProviderEvidence {
			changed := valid[1]
			changed.Lane = "unexpected"
			return changed
		}()},
	}
	for name, providers := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := NewEvidence(profile, selection, artifactDigestA, fixtureLatencyMS, score, providers); err == nil {
				t.Fatal("bad provider coverage accepted")
			}
		})
	}
}

func TestNewEvidenceRejectsIdentityAndScoreTamper(t *testing.T) {
	profile, selection := selectFixture(t)
	score, err := Aggregate(selection, balancedOutcomes(selection, profile.CasesPerCapability/2))
	if err != nil {
		t.Fatal(err)
	}
	providers := providerEvidenceFor(profile)

	badArtifact := strings.Repeat("A", 64)
	if _, err := NewEvidence(profile, selection, badArtifact, fixtureLatencyMS, score, providers); err == nil {
		t.Fatal("uppercase artifact digest accepted")
	}

	changedSelection := selection
	changedSelection.Cases = append([]SelectedCase(nil), selection.Cases...)
	changedSelection.Cases[0].QuestionID += "-tampered"
	if _, err := NewEvidence(profile, changedSelection, artifactDigestA, fixtureLatencyMS, score, providers); err == nil {
		t.Fatal("tampered selection accepted")
	}

	changedScore := score
	changedScore.PerCapability = append([]CapabilityScore(nil), score.PerCapability...)
	changedScore.PerCapability[0].Correct++
	if _, err := NewEvidence(profile, selection, artifactDigestA, fixtureLatencyMS, changedScore, providers); err == nil {
		t.Fatal("tampered score accepted")
	}

	wrongCount := score
	wrongCount.PerCapability = append([]CapabilityScore(nil), score.PerCapability...)
	wrongCount.PerCapability[0].Count--
	if _, err := NewEvidence(profile, selection, artifactDigestA, fixtureLatencyMS, wrongCount, providers); err == nil {
		t.Fatal("selection/score capability mismatch accepted")
	}
}

func TestEvidenceDigestIsCanonicalAcrossProviderInputOrder(t *testing.T) {
	profile, selection, _, evidence := validEvidence(t)
	want, err := evidence.Digest(profile, selection)
	if err != nil {
		t.Fatal(err)
	}
	reordered := cloneEvidence(evidence)
	reordered.ProviderEvidence[0], reordered.ProviderEvidence[1] = reordered.ProviderEvidence[1], reordered.ProviderEvidence[0]
	got, err := reordered.Digest(profile, selection)
	if err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("provider input order moved digest: got %s want %s", got, want)
	}
}

func TestEvidenceDigestDetectsEveryMaterialTamper(t *testing.T) {
	profile, selection, _, evidence := validEvidence(t)
	want, err := evidence.Digest(profile, selection)
	if err != nil {
		t.Fatal(err)
	}
	tests := map[string]func(*Evidence){
		"artifact":           func(e *Evidence) { e.ArtifactSHA256 = artifactDigestB },
		"profile":            func(e *Evidence) { e.ProfileChecksum = strings.Repeat("e", 64) },
		"case set":           func(e *Evidence) { e.CaseSetDigest = strings.Repeat("f", 64) },
		"dataset revision":   func(e *Evidence) { e.DatasetRevision += "-changed" },
		"dataset digest":     func(e *Evidence) { e.DatasetSHA256 = strings.Repeat("1", 64) },
		"mean":               func(e *Evidence) { e.Score.LongMemMean += 0.01 },
		"uncertainty":        func(e *Evidence) { e.Score.LongMemStdErr += 0.01 },
		"case count":         func(e *Evidence) { e.Score.CaseCount++ },
		"capability correct": func(e *Evidence) { e.Score.PerCapability[0].Correct++ },
		"capability count":   func(e *Evidence) { e.Score.PerCapability[0].Count++ },
		"capability mean":    func(e *Evidence) { e.Score.PerCapability[0].Mean += 0.1 },
		"provider":           func(e *Evidence) { e.ProviderEvidence[0].Provider += "-changed" },
		"model":              func(e *Evidence) { e.ProviderEvidence[0].Model += "-changed" },
		"fallback":           func(e *Evidence) { e.ProviderEvidence[0].FallbackUsed = true },
		"requests":           func(e *Evidence) { e.ProviderEvidence[0].Requests++ },
		"tokens":             func(e *Evidence) { e.ProviderEvidence[0].PromptTokens++ },
		"cost":               func(e *Evidence) { e.ProviderEvidence[0].CostUSDmicros++ },
		"receipt":            func(e *Evidence) { e.ProviderEvidence[0].ReceiptSetSHA256 = strings.Repeat("2", 64) },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			changed := cloneEvidence(evidence)
			mutate(&changed)
			got, err := changed.Digest(profile, selection)
			if err != nil {
				// Rejecting malformed tamper is at least as strong as moving the digest.
				return
			}
			if got == want {
				t.Fatal("material tamper did not move evidence digest")
			}
		})
	}
}

func TestEvidenceIdentityUsesArtifactDigestNotFilename(t *testing.T) {
	profile, selection, _, first := validEvidence(t)
	// No filename exists in Evidence. Two scheduler records named differently
	// but resolving to identical bytes must therefore canonicalize identically.
	second := cloneEvidence(first)
	firstDigest, err := first.Digest(profile, selection)
	if err != nil {
		t.Fatal(err)
	}
	secondDigest, err := second.Digest(profile, selection)
	if err != nil {
		t.Fatal(err)
	}
	if firstDigest != secondDigest || !reflect.DeepEqual(first, second) {
		t.Fatal("renamed identical artifact changed evidence identity")
	}
	second.ArtifactSHA256 = artifactDigestB
	changedDigest, err := second.Digest(profile, selection)
	if err != nil {
		t.Fatal(err)
	}
	if changedDigest == firstDigest {
		t.Fatal("different artifact bytes reused evidence identity")
	}
}

func TestEvidenceDigestRejectsIncompleteOrNonFiniteEvidence(t *testing.T) {
	profile, selection, _, valid := validEvidence(t)
	tests := map[string]func(*Evidence){
		"schema":           func(e *Evidence) { e.SchemaVersion++ },
		"bench":            func(e *Evidence) { e.BenchVersion = 8 },
		"artifact":         func(e *Evidence) { e.ArtifactSHA256 = "bad" },
		"profile":          func(e *Evidence) { e.ProfileChecksum = "bad" },
		"case set":         func(e *Evidence) { e.CaseSetDigest = "bad" },
		"dataset":          func(e *Evidence) { e.DatasetSHA256 = "bad" },
		"dataset revision": func(e *Evidence) { e.DatasetRevision = "" },
		"NaN mean":         func(e *Evidence) { e.Score.LongMemMean = math.NaN() },
		"Inf stderr":       func(e *Evidence) { e.Score.LongMemStdErr = math.Inf(1) },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			changed := cloneEvidence(valid)
			mutate(&changed)
			if _, err := changed.Digest(profile, selection); err == nil {
				t.Fatal("incomplete evidence accepted")
			}
		})
	}
}

func TestEvidenceDigestRevalidatesPostConstructionMutation(t *testing.T) {
	profile, selection, _, valid := validEvidence(t)
	tests := map[string]func(*Evidence){
		"provider drift": func(e *Evidence) { e.ProviderEvidence[0].Provider += "-fallback" },
		"fallback":       func(e *Evidence) { e.ProviderEvidence[0].FallbackUsed = true },
		"estimated cost": func(e *Evidence) { e.ProviderEvidence[0].CostSource = "model_list_price" },
		"cost over cap": func(e *Evidence) {
			lane := e.ProviderEvidence[0].Lane
			for _, policy := range profile.Providers {
				if policy.Lane == lane {
					e.ProviderEvidence[0].CostUSDmicros = policy.MaxCostUSDmicros + 1
				}
			}
		},
		"capability cardinality": func(e *Evidence) { e.Score.PerCapability[0].Count-- },
		"macro mean":             func(e *Evidence) { e.Score.LongMemMean += 0.01 },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			changed := cloneEvidence(valid)
			mutate(&changed)
			if err := changed.Validate(profile, selection); err == nil {
				t.Fatal("mutated evidence validated")
			}
			if _, err := changed.Digest(profile, selection); err == nil {
				t.Fatal("mutated evidence became signable")
			}
		})
	}
}

func TestPublicEvidenceContainsNoRawQuestionIdentifiers(t *testing.T) {
	profile, selection, _, evidence := validEvidence(t)
	if err := evidence.Validate(profile, selection); err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(evidence)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(raw, []byte("question_id")) {
		t.Fatalf("public evidence exposes question_id field: %s", raw)
	}
	for _, selected := range selection.Cases {
		if bytes.Contains(raw, []byte(selected.QuestionID)) {
			t.Fatalf("public evidence exposes selected question %q", selected.QuestionID)
		}
	}
	if !bytes.Contains(raw, []byte(selection.CaseSetDigest)) {
		t.Fatal("public evidence omitted opaque case-set digest")
	}
}
