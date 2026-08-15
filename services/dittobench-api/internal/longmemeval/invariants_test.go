package longmemeval

import (
	"encoding/json"
	"fmt"
	"math"
	"math/rand"
	"reflect"
	"sort"
	"strings"
	"testing"
)

func TestSelectionPropertiesAcrossSeedsQuotasAndPermutations(t *testing.T) {
	base := loadTestProfile(t)
	allCases := syntheticCases(30)
	for _, quota := range []int{2, 3, 5, 10, 20} {
		for _, seed := range []uint64{0, 1, 385, 1<<32 + 7, ^uint64(0)} {
			name := fmt.Sprintf("quota-%d-seed-%d", quota, seed)
			t.Run(name, func(t *testing.T) {
				profile := base
				profile.CasesPerCapability = quota
				profile.SelectionSeed = seed
				reference, err := Select(profile, allCases)
				if err != nil {
					t.Fatal(err)
				}
				assertSelectionInvariants(t, profile, allCases, reference)

				for permutation := int64(0); permutation < 12; permutation++ {
					shuffled := append([]Case(nil), allCases...)
					rand.New(rand.NewSource(permutation)).Shuffle(len(shuffled), func(i, j int) {
						shuffled[i], shuffled[j] = shuffled[j], shuffled[i]
					})
					got, err := Select(profile, shuffled)
					if err != nil {
						t.Fatal(err)
					}
					if !reflect.DeepEqual(got, reference) {
						t.Fatalf("permutation %d changed selection", permutation)
					}
				}
			})
		}
	}
}

func assertSelectionInvariants(t *testing.T, profile Profile, dataset []Case, selection Selection) {
	t.Helper()
	checksum, err := profile.Checksum()
	if err != nil {
		t.Fatal(err)
	}
	if selection.ProfileChecksum != checksum || selection.DatasetSHA256 != profile.DatasetSHA256 {
		t.Fatal("selection identity does not match profile")
	}
	if selection.CaseSetDigest != caseSetDigest(selection) {
		t.Fatal("selection carries a stale case-set digest")
	}
	if len(selection.Cases) != len(capabilityOrder)*profile.CasesPerCapability {
		t.Fatalf("selection count=%d", len(selection.Cases))
	}

	datasetByID := make(map[string]Case, len(dataset))
	for _, item := range dataset {
		datasetByID[item.QuestionID] = item
	}
	seen := make(map[string]struct{}, len(selection.Cases))
	for capabilityIndex, capability := range capabilityOrder {
		start := capabilityIndex * profile.CasesPerCapability
		end := start + profile.CasesPerCapability
		for _, item := range selection.Cases[start:end] {
			if item.Capability != capability {
				t.Fatalf("noncanonical capability order at %d: %q", start, item.Capability)
			}
			if _, duplicate := seen[item.QuestionID]; duplicate {
				t.Fatalf("question selected twice: %q", item.QuestionID)
			}
			seen[item.QuestionID] = struct{}{}
			source, ok := datasetByID[item.QuestionID]
			if !ok {
				t.Fatalf("selected question absent from dataset: %q", item.QuestionID)
			}
			mapped, err := capabilityForCase(source)
			if err != nil {
				t.Fatal(err)
			}
			if mapped != item.Capability {
				t.Fatalf("question %q mapped to %q but reported %q", item.QuestionID, mapped, item.Capability)
			}
		}
	}
}

func TestSelectionDigestMovesForEveryProfileChange(t *testing.T) {
	base := loadTestProfile(t)
	dataset := syntheticCases(30)
	reference, err := Select(base, dataset)
	if err != nil {
		t.Fatal(err)
	}
	mutations := map[string]func(*Profile){
		"revision":         func(p *Profile) { p.Revision += "-next" },
		"dataset revision": func(p *Profile) { p.DatasetRevision += "-next" },
		"dataset digest":   func(p *Profile) { p.DatasetSHA256 = strings.Repeat("9", 64) },
		"seed":             func(p *Profile) { p.SelectionSeed++ },
		"quota":            func(p *Profile) { p.CasesPerCapability++ },
		"provider profile": func(p *Profile) { p.Providers[0].ProfileRevision += "-next" },
		"provider model":   func(p *Profile) { p.Providers[0].Model += "-next" },
		"request budget":   func(p *Profile) { p.Providers[0].MaxRequests++ },
		"token budget":     func(p *Profile) { p.Providers[0].MaxTotalTokens++ },
		"cost budget":      func(p *Profile) { p.Providers[0].MaxCostUSDmicros++ },
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			changed := base
			changed.Providers = append([]ProviderPolicy(nil), base.Providers...)
			mutate(&changed)
			selection, err := Select(changed, dataset)
			if err != nil {
				t.Fatal(err)
			}
			if selection.ProfileChecksum == reference.ProfileChecksum {
				t.Fatal("profile change preserved checksum")
			}
			if selection.CaseSetDigest == reference.CaseSetDigest {
				t.Fatal("profile change preserved case-set digest")
			}
		})
	}
}

func TestSelectionUsesSmallestHashesWithinEachStratum(t *testing.T) {
	profile := loadTestProfile(t)
	profile.CasesPerCapability = 4
	dataset := syntheticCases(20)
	selection, err := Select(profile, dataset)
	if err != nil {
		t.Fatal(err)
	}
	checksum, err := profile.Checksum()
	if err != nil {
		t.Fatal(err)
	}

	for _, capability := range capabilityOrder {
		var candidates []rankedCase
		for _, item := range dataset {
			mapped, err := capabilityForCase(item)
			if err != nil {
				t.Fatal(err)
			}
			if mapped == capability {
				candidates = append(candidates, rankedCase{
					Case: item,
					Rank: selectionRank(checksum, profile.SelectionSeed, item.QuestionID),
				})
			}
		}
		sort.Slice(candidates, func(i, j int) bool {
			return strings.Compare(fmt.Sprintf("%x", candidates[i].Rank), fmt.Sprintf("%x", candidates[j].Rank)) < 0
		})
		want := make(map[string]struct{}, profile.CasesPerCapability)
		for _, item := range candidates[:profile.CasesPerCapability] {
			want[item.Case.QuestionID] = struct{}{}
		}
		for _, selected := range selection.Cases {
			if selected.Capability != capability {
				continue
			}
			if _, ok := want[selected.QuestionID]; !ok {
				t.Fatalf("%q was not among smallest hashes for %q", selected.QuestionID, capability)
			}
		}
	}
}

func TestAggregateUniformRatesAcrossEntireDomain(t *testing.T) {
	profile, selection := selectFixture(t)
	for correct := 0; correct <= profile.CasesPerCapability; correct++ {
		t.Run(fmt.Sprintf("correct-%d", correct), func(t *testing.T) {
			score, err := Aggregate(selection, balancedOutcomes(selection, correct))
			if err != nil {
				t.Fatal(err)
			}
			mean := float64(correct) / float64(profile.CasesPerCapability)
			if score.LongMemMean != round6(mean) {
				t.Fatalf("mean=%v want=%v", score.LongMemMean, round6(mean))
			}
			sampleVariance := mean * (1 - mean) * float64(profile.CasesPerCapability) /
				float64(profile.CasesPerCapability-1)
			wantStdErr := round6(math.Sqrt(sampleVariance / float64(profile.CasesPerCapability*len(capabilityOrder))))
			if score.LongMemStdErr != wantStdErr {
				t.Fatalf("stderr=%v want=%v", score.LongMemStdErr, wantStdErr)
			}
			if err := validateScore(selection, score); err != nil {
				t.Fatalf("aggregate did not validate: %v", err)
			}
		})
	}
}

func TestAggregateSingleCapabilityPerturbationHasEqualWeight(t *testing.T) {
	_, selection := selectFixture(t)
	for _, winningCapability := range capabilityOrder {
		t.Run(string(winningCapability), func(t *testing.T) {
			outcomes := make([]Outcome, 0, len(selection.Cases))
			won := false
			for _, item := range selection.Cases {
				correct := item.Capability == winningCapability && !won
				if correct {
					won = true
				}
				outcomes = append(outcomes, Outcome{QuestionID: item.QuestionID, Correct: correct})
			}
			score, err := Aggregate(selection, outcomes)
			if err != nil {
				t.Fatal(err)
			}
			if score.LongMemMean != round6(1.0/60.0) {
				t.Fatalf("capability %q got unequal macro weight %v", winningCapability, score.LongMemMean)
			}
		})
	}
}

func TestProviderReceiptsCoverFailuresAsWellAsSuccesses(t *testing.T) {
	profile := loadTestProfile(t)
	policy := profile.Providers[0]
	evidence := providerEvidenceFor(profile)[0]
	evidence.Requests = 3
	evidence.Successes = 2
	evidence.ReceiptedRequests = 2
	if err := ValidateProviderEvidence(policy, evidence); err == nil {
		t.Fatal("unreceipted failed provider attempt accepted")
	}
	evidence.ReceiptedRequests = 3
	if err := ValidateProviderEvidence(policy, evidence); err != nil {
		t.Fatalf("fully receipted retry ledger rejected: %v", err)
	}
}

func TestEveryProviderLaneCanValidateIndependently(t *testing.T) {
	profile := loadTestProfile(t)
	evidence := providerEvidenceFor(profile)
	if len(evidence) != len(profile.Providers) {
		t.Fatal("test fixture provider coverage mismatch")
	}
	for _, policy := range profile.Providers {
		t.Run(policy.Lane, func(t *testing.T) {
			var found *ProviderEvidence
			for index := range evidence {
				if evidence[index].Lane == policy.Lane {
					found = &evidence[index]
				}
			}
			if found == nil {
				t.Fatal("missing provider evidence")
			}
			if err := ValidateProviderEvidence(policy, *found); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestEvidenceJSONRoundTripRemainsValidatedAndCanonical(t *testing.T) {
	profile, selection, _, evidence := validEvidence(t)
	wantDigest, err := evidence.Digest(profile, selection)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(evidence)
	if err != nil {
		t.Fatal(err)
	}
	var decoded Evidence
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatal(err)
	}
	if err := decoded.Validate(profile, selection); err != nil {
		t.Fatalf("JSON round trip failed validation: %v", err)
	}
	gotDigest, err := decoded.Digest(profile, selection)
	if err != nil {
		t.Fatal(err)
	}
	if gotDigest != wantDigest {
		t.Fatalf("JSON round trip moved digest: got %s want %s", gotDigest, wantDigest)
	}
}

func TestEvidenceHasFrozenGoldenDigest(t *testing.T) {
	profile, selection, _, evidence := validEvidence(t)
	got, err := evidence.Digest(profile, selection)
	if err != nil {
		t.Fatal(err)
	}
	const want = "7194baa74fcd68d6ea3466b95ba2b9f5e995e0138151b57697382f009b4b6197"
	if got != want {
		t.Fatalf("canonical evidence digest changed: got %s want %s", got, want)
	}
}

func TestEvidenceTopLevelIdentityMustMatchProfileAndSelection(t *testing.T) {
	profile, selection, _, valid := validEvidence(t)
	tests := map[string]func(*Evidence){
		"schema":           func(e *Evidence) { e.SchemaVersion++ },
		"bench":            func(e *Evidence) { e.BenchVersion-- },
		"profile checksum": func(e *Evidence) { e.ProfileChecksum = strings.Repeat("3", 64) },
		"case set":         func(e *Evidence) { e.CaseSetDigest = strings.Repeat("4", 64) },
		"dataset revision": func(e *Evidence) { e.DatasetRevision += "-other" },
		"dataset digest":   func(e *Evidence) { e.DatasetSHA256 = strings.Repeat("5", 64) },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			changed := cloneEvidence(valid)
			mutate(&changed)
			if err := changed.Validate(profile, selection); err == nil {
				t.Fatal("contradictory evidence identity accepted")
			}
			if _, err := changed.Digest(profile, selection); err == nil {
				t.Fatal("contradictory evidence identity became signable")
			}
		})
	}
}

func TestEvidenceUsesIntegerMicrousdAccounting(t *testing.T) {
	profile, selection, _, evidence := validEvidence(t)
	if err := evidence.Validate(profile, selection); err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(evidence)
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	if strings.Contains(text, "estimated_cost") || strings.Contains(text, "list_price") {
		t.Fatalf("evidence contains estimated price field: %s", text)
	}
	if !strings.Contains(text, `"cost_source":"provider_receipt_v1"`) ||
		!strings.Contains(text, `"cost_usd_micros":500`) {
		t.Fatalf("evidence omitted authoritative integer cost: %s", text)
	}
}

func TestProfileFixtureIsExplicitlyShadowOnly(t *testing.T) {
	profile := loadTestProfile(t)
	if !strings.Contains(profile.Revision, "shadow") {
		t.Fatalf("uncalibrated fixture must not claim launch status: %q", profile.Revision)
	}
	var reader ProviderPolicy
	for _, policy := range profile.Providers {
		if policy.Lane == "reader" {
			reader = policy
		}
	}
	if reader.Model != "openai/gpt-oss-20b" {
		t.Fatalf("shadow reader model=%q", reader.Model)
	}
}

func TestPrivateSelectionAndPublicEvidenceHaveSeparateShapes(t *testing.T) {
	_, selection, _, evidence := validEvidence(t)
	selectionJSON, err := json.Marshal(selection)
	if err != nil {
		t.Fatal(err)
	}
	evidenceJSON, err := json.Marshal(evidence)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(selectionJSON), "question_id") {
		t.Fatal("private selection unexpectedly lost its grading identity")
	}
	if strings.Contains(string(evidenceJSON), "question_id") {
		t.Fatal("public evidence inherited private selection identity")
	}
	if !strings.Contains(string(evidenceJSON), selection.CaseSetDigest) {
		t.Fatal("public evidence did not retain opaque case-set commitment")
	}
}

func FuzzSelectionOrderIndependence(f *testing.F) {
	for _, seed := range []uint64{0, 1, 385, 99999, ^uint64(0)} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, seed uint64) {
		profile := loadTestProfile(t)
		profile.SelectionSeed = seed
		dataset := syntheticCases(20)
		want, err := Select(profile, dataset)
		if err != nil {
			t.Fatal(err)
		}
		rand.New(rand.NewSource(int64(seed))).Shuffle(len(dataset), func(i, j int) {
			dataset[i], dataset[j] = dataset[j], dataset[i]
		})
		got, err := Select(profile, dataset)
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(got, want) {
			t.Fatal("input permutation changed selection")
		}
	})
}

func FuzzProviderEvidenceNeverAcceptsUnreceiptedRequests(f *testing.F) {
	f.Add(uint64(2), uint64(2))
	f.Add(uint64(3), uint64(2))
	f.Add(uint64(10), uint64(1))
	f.Fuzz(func(t *testing.T, requests, receipted uint64) {
		profile := loadTestProfile(t)
		policy := profile.Providers[0]
		if requests == 0 || requests > policy.MaxRequests || receipted > policy.MaxRequests {
			t.Skip()
		}
		evidence := providerEvidenceFor(profile)[0]
		evidence.Requests = requests
		evidence.Successes = 1
		evidence.ReceiptedRequests = receipted
		err := ValidateProviderEvidence(policy, evidence)
		if receipted != requests && err == nil {
			t.Fatal("unreceipted provider requests accepted")
		}
	})
}
