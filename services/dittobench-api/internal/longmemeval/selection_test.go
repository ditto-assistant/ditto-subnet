package longmemeval

import (
	"math/rand"
	"reflect"
	"strings"
	"testing"
)

func TestSelectionIsOrderIndependentAndBalanced(t *testing.T) {
	profile := loadTestProfile(t)
	cases := syntheticCases(profile.CasesPerCapability + 7)
	want, err := Select(profile, cases)
	if err != nil {
		t.Fatal(err)
	}

	shuffled := append([]Case(nil), cases...)
	rand.New(rand.NewSource(7)).Shuffle(len(shuffled), func(i, j int) {
		shuffled[i], shuffled[j] = shuffled[j], shuffled[i]
	})
	got, err := Select(profile, shuffled)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("input order changed selection:\n got %#v\nwant %#v", got, want)
	}
	if !validSHA256(got.CaseSetDigest) {
		t.Fatalf("invalid case-set digest %q", got.CaseSetDigest)
	}
	if len(got.Cases) != 6*profile.CasesPerCapability {
		t.Fatalf("selected %d cases", len(got.Cases))
	}

	counts := make(map[Capability]int)
	for _, item := range got.Cases {
		counts[item.Capability]++
	}
	for _, capability := range Capabilities() {
		if counts[capability] != profile.CasesPerCapability {
			t.Fatalf("%s count=%d", capability, counts[capability])
		}
	}
}

func TestAbstentionsAreRemovedFromSourceStrata(t *testing.T) {
	profile := loadTestProfile(t)
	profile.CasesPerCapability = 2
	cases := []Case{
		{QuestionID: "u", QuestionType: "single-session-user"},
		{QuestionID: "a", QuestionType: "single-session-assistant"},
		{QuestionID: "u_abs", QuestionType: "single-session-user"},
		{QuestionID: "m_abs", QuestionType: "multi-session"},
		{QuestionID: "m1", QuestionType: "multi-session"},
		{QuestionID: "m2", QuestionType: "multi-session"},
		{QuestionID: "t1", QuestionType: "temporal-reasoning"},
		{QuestionID: "t2", QuestionType: "temporal-reasoning"},
		{QuestionID: "k1", QuestionType: "knowledge-update"},
		{QuestionID: "k2", QuestionType: "knowledge-update"},
		{QuestionID: "p1", QuestionType: "single-session-preference"},
		{QuestionID: "p2", QuestionType: "single-session-preference"},
	}
	selection, err := Select(profile, cases)
	if err != nil {
		t.Fatal(err)
	}
	for _, item := range selection.Cases {
		if strings.HasSuffix(item.QuestionID, "_abs") && item.Capability != CapabilityAbstention {
			t.Fatalf("abstention leaked into %q", item.Capability)
		}
		if !strings.HasSuffix(item.QuestionID, "_abs") && item.Capability == CapabilityAbstention {
			t.Fatalf("ordinary case %q entered abstention", item.QuestionID)
		}
	}
}

func TestSelectionIsCommonAcrossArtifacts(t *testing.T) {
	profile := loadTestProfile(t)
	selectionA, err := Select(profile, syntheticCases(20))
	if err != nil {
		t.Fatal(err)
	}
	// Artifact identity is intentionally not an input. The scheduler may bind
	// this same common selection to any number of candidate artifacts.
	selectionB, err := Select(profile, syntheticCases(20))
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(selectionA, selectionB) {
		t.Fatal("common profile produced different candidate case sets")
	}
}

func TestSelectionMovesWithFrozenProfileSeed(t *testing.T) {
	profile := loadTestProfile(t)
	cases := syntheticCases(40)
	first, err := Select(profile, cases)
	if err != nil {
		t.Fatal(err)
	}
	profile.SelectionSeed++
	second, err := Select(profile, cases)
	if err != nil {
		t.Fatal(err)
	}
	if first.ProfileChecksum == second.ProfileChecksum || first.CaseSetDigest == second.CaseSetDigest {
		t.Fatal("selection seed failed to move frozen identities")
	}
	if reflect.DeepEqual(first.Cases, second.Cases) {
		t.Fatal("selection seed unexpectedly chose identical cases")
	}
}

func TestSelectionCapabilityMapping(t *testing.T) {
	tests := []struct {
		name string
		item Case
		want Capability
	}{
		{"user extraction", Case{"q1", "single-session-user"}, CapabilityExtraction},
		{"assistant extraction", Case{"q2", "single-session-assistant"}, CapabilityExtraction},
		{"multi", Case{"q3", "multi-session"}, CapabilityMultiSessionReasoning},
		{"temporal", Case{"q4", "temporal-reasoning"}, CapabilityTemporalReasoning},
		{"update", Case{"q5", "knowledge-update"}, CapabilityKnowledgeUpdate},
		{"preference", Case{"q6", "single-session-preference"}, CapabilityPreference},
		{"abstention overrides type", Case{"q7_abs", "knowledge-update"}, CapabilityAbstention},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := capabilityForCase(test.item)
			if err != nil {
				t.Fatal(err)
			}
			if got != test.want {
				t.Fatalf("got %q want %q", got, test.want)
			}
		})
	}
}

func TestSelectionRejectsMalformedDataset(t *testing.T) {
	profile := loadTestProfile(t)
	valid := syntheticCases(profile.CasesPerCapability + 1)
	tests := map[string][]Case{
		"empty":        nil,
		"empty id":     append(valid, Case{QuestionType: "multi-session"}),
		"duplicate":    append(valid, valid[0]),
		"unknown type": append(valid, Case{QuestionID: "unknown", QuestionType: "secret-type"}),
	}
	for name, cases := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := Select(profile, cases); err == nil {
				t.Fatal("malformed dataset accepted")
			}
		})
	}
}

func TestSelectionRejectsEveryUnderfilledStratum(t *testing.T) {
	base := loadTestProfile(t)
	for _, missing := range Capabilities() {
		t.Run(string(missing), func(t *testing.T) {
			cases := syntheticCases(base.CasesPerCapability)
			filtered := make([]Case, 0, len(cases)-1)
			removed := false
			for _, item := range cases {
				capability, err := capabilityForCase(item)
				if err != nil {
					t.Fatal(err)
				}
				if capability == missing && !removed {
					removed = true
					continue
				}
				filtered = append(filtered, item)
			}
			if _, err := Select(base, filtered); err == nil {
				t.Fatal("underfilled stratum accepted")
			}
		})
	}
}

func TestCaseSetDigestDetectsOrderAndMembershipTamper(t *testing.T) {
	_, selection := selectFixture(t)
	want := selection.CaseSetDigest

	reordered := selection
	reordered.Cases = append([]SelectedCase(nil), selection.Cases...)
	reordered.Cases[0], reordered.Cases[1] = reordered.Cases[1], reordered.Cases[0]
	if caseSetDigest(reordered) == want {
		t.Fatal("order tamper did not move case-set digest")
	}

	renamed := selection
	renamed.Cases = append([]SelectedCase(nil), selection.Cases...)
	renamed.Cases[0].QuestionID += "-changed"
	if caseSetDigest(renamed) == want {
		t.Fatal("membership tamper did not move case-set digest")
	}

	reclassified := selection
	reclassified.Cases = append([]SelectedCase(nil), selection.Cases...)
	reclassified.Cases[0].Capability = CapabilityPreference
	if caseSetDigest(reclassified) == want {
		t.Fatal("capability tamper did not move case-set digest")
	}
}
