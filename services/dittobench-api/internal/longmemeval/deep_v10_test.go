package longmemeval

import (
	"fmt"
	"strings"
	"testing"
)

func v10DeepProfile(t *testing.T) Profile {
	t.Helper()
	profile := loadTestProfile(t)
	profile.Revision = "bench-v10-deep-history-test-v1"
	profile.BenchVersion = 10
	profile.CasesPerCapability = V10MinCasesPerCapability
	profile.MinHistorySessions = V10MinHistorySessions
	profile.MinHistoryBytes = V10MinHistoryBytes
	return profile
}

func TestV10DeepProfileFailsClosedBelowEveryFloor(t *testing.T) {
	base := v10DeepProfile(t)
	if err := base.Validate(); err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(*Profile){
		"version":  func(profile *Profile) { profile.BenchVersion = 8 },
		"cases":    func(profile *Profile) { profile.CasesPerCapability-- },
		"sessions": func(profile *Profile) { profile.MinHistorySessions-- },
		"bytes":    func(profile *Profile) { profile.MinHistoryBytes-- },
	} {
		t.Run(name, func(t *testing.T) {
			changed := base
			if mutate(&changed); changed.Validate() == nil {
				t.Fatal("profile below the v10 deep-history floor was accepted")
			}
		})
	}
}

func TestV10SelectionUsesIndependentDomainAndProjectsVersion(t *testing.T) {
	profile := v10DeepProfile(t)
	var cases []Case
	for index := 0; index < V10MinCasesPerCapability; index++ {
		cases = append(cases,
			Case{QuestionID: fmt.Sprintf("extract-%02d", index), QuestionType: "single-session-user"},
			Case{QuestionID: fmt.Sprintf("multi-%02d", index), QuestionType: "multi-session"},
			Case{QuestionID: fmt.Sprintf("temporal-%02d", index), QuestionType: "temporal-reasoning"},
			Case{QuestionID: fmt.Sprintf("update-%02d", index), QuestionType: "knowledge-update"},
			Case{QuestionID: fmt.Sprintf("preference-%02d", index), QuestionType: "single-session-preference"},
			Case{QuestionID: fmt.Sprintf("abstain-%02d_abs", index), QuestionType: "multi-session"},
		)
	}
	selection, err := Select(profile, cases)
	if err != nil {
		t.Fatal(err)
	}
	if selection.BenchVersion != 10 || len(selection.Cases) != 6*V10MinCasesPerCapability {
		t.Fatalf("v10 selection version/count=%d/%d", selection.BenchVersion, len(selection.Cases))
	}
	if selectionRankForVersion(10, selection.ProfileChecksum, profile.SelectionSeed, cases[0].QuestionID) ==
		selectionRankForVersion(9, selection.ProfileChecksum, profile.SelectionSeed, cases[0].QuestionID) {
		t.Fatal("v10 selection reused the v9 ranking domain")
	}

	dataset := LoadedDataset{Revision: profile.DatasetRevision, SHA256: profile.DatasetSHA256, Selection: selection, selected: map[string]DatasetCase{}}
	for _, selected := range selection.Cases {
		dataset.selected[selected.QuestionID] = DatasetCase{
			QuestionID: selected.QuestionID, QuestionType: "multi-session", Question: "What changed?", QuestionDate: "2026/01/02 (Fri) 10:00",
			HaystackSessionIDs: []string{"session"}, HaystackDates: []string{"2025/01/02 (Thu) 10:00"},
			HaystackSessions: [][]DatasetTurn{{{Role: "user", Content: "A remembered fact."}}},
		}
	}
	projected, err := ProjectSelectedCases(dataset, []byte(strings.Repeat("k", 32)), 4)
	if err != nil {
		t.Fatal(err)
	}
	for _, item := range projected {
		if item.RunRequest.BenchVersion != 10 {
			t.Fatalf("projected request version=%d, want 10", item.RunRequest.BenchVersion)
		}
	}
	outcomes := make([]Outcome, 0, len(selection.Cases))
	for _, selected := range selection.Cases {
		outcomes = append(outcomes, Outcome{QuestionID: selected.QuestionID, Correct: true})
	}
	score, err := Aggregate(selection, outcomes)
	if err != nil {
		t.Fatal(err)
	}
	evidence, err := NewEvidence(profile, selection, artifactDigestA, fixtureLatencyMS, score, providerEvidenceFor(profile))
	if err != nil {
		t.Fatal(err)
	}
	if evidence.BenchVersion != 10 || evidence.Validate(profile, selection) != nil {
		t.Fatal("v10 deep-history score did not produce replay-valid evidence")
	}
}

func TestV10SelectedRowsEnforceHistoryDepth(t *testing.T) {
	profile := v10DeepProfile(t)
	sessions := make([][]DatasetTurn, profile.MinHistorySessions)
	for index := range sessions {
		sessions[index] = []DatasetTurn{{Role: "user", Content: strings.Repeat("x", profile.MinHistoryBytes/profile.MinHistorySessions+1)}}
	}
	entry := DatasetCase{QuestionID: "deep-case", HaystackSessions: sessions}
	if err := validateProfileHistoryFloor(entry, profile); err != nil {
		t.Fatal(err)
	}
	entry.HaystackSessions = entry.HaystackSessions[:len(entry.HaystackSessions)-1]
	if err := validateProfileHistoryFloor(entry, profile); err == nil {
		t.Fatal("short history session count was accepted")
	}
}
