package ablation

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"math"
	"reflect"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const (
	testDatasetSHA  = "11edff888858054abd4e89a76169f3b88cd42b9b236377fda4b3bca3a8205c00"
	testManifestSHA = "22edff888858054abd4e89a76169f3b88cd42b9b236377fda4b3bca3a8205c00"
	testArtifactSHA = "33edff888858054abd4e89a76169f3b88cd42b9b236377fda4b3bca3a8205c00"
)

var testProjectionNow = time.Date(2026, time.August, 10, 16, 30, 45, 0, time.UTC)

func inferenceBudget(requests, inputBytes uint64) Budget {
	return Budget{MaxChatRequests: requests, MaxChatInputBytes: inputBytes}
}

func embeddingBudget(requests, inputs, inputBytes uint64) Budget {
	return Budget{
		MaxEmbeddingRequests: requests, MaxEmbeddingInputs: inputs,
		MaxEmbeddingInputBytes: inputBytes,
	}
}

func mustLedger(t *testing.T, intervention Intervention, budget Budget) *Ledger {
	t.Helper()
	ledger, err := NewLedger(intervention, budget)
	if err != nil {
		t.Fatalf("NewLedger: %v", err)
	}
	return ledger
}

func usageWithCalls(t *testing.T, intervention Intervention, calls int) Usage {
	t.Helper()
	var ledger *Ledger
	if intervention == InterventionInference {
		ledger = mustLedger(t, intervention, inferenceBudget(uint64(calls+2), 4096))
		for range calls {
			if err := ledger.AdmitChat(10); err != nil {
				t.Fatalf("AdmitChat: %v", err)
			}
		}
	} else {
		ledger = mustLedger(t, intervention, embeddingBudget(uint64(calls+2), uint64(calls+2), 4096))
		for range calls {
			if err := ledger.AdmitEmbedding(1, 10); err != nil {
				t.Fatalf("AdmitEmbedding: %v", err)
			}
		}
	}
	return ledger.Snapshot()
}

func evaluationInput(intervention Intervention, mode Mode, usage Usage) EvaluateInput {
	input := EvaluateInput{
		BenchVersion: BenchVersionV9, ArtifactSHA256: testArtifactSHA,
		Intervention: intervention, Mode: mode,
		ProfileRevision: "launch-v1", ThresholdManifestSHA256: testManifestSHA,
		DatasetSHA256: testDatasetSHA, Threshold: 0.2,
		Baseline: []CaseScore{{CaseID: "case-c", Score: 0.8}, {CaseID: "case-a", Score: 1}, {CaseID: "case-b", Score: 0.9}},
		Ablated:  []CaseScore{{CaseID: "case-c", Score: 0.5}, {CaseID: "case-a", Score: 0.3}, {CaseID: "case-b", Score: 0.4}},
		Usage:    usage,
	}
	policy := CoordinatorPolicy{
		SampleSize: len(input.Baseline), MaxAttempts: 3, MaxRequests: len(input.Baseline) * 9,
		RequestTimeoutMilliseconds: 100, TotalTimeoutMilliseconds: 2000,
	}
	inferenceBudgetValue := inferenceBudget(5, 4096)
	embeddingBudgetValue := embeddingBudget(5, 5, 4096)
	if intervention == InterventionInference {
		inferenceBudgetValue = usage.Budget
	} else {
		embeddingBudgetValue = usage.Budget
	}
	input.FrozenProfile = FrozenProfile{
		ContractVersion: ProfileContractVersion, Revision: input.ProfileRevision, BenchVersion: input.BenchVersion,
		DatasetSHA256:           input.DatasetSHA256,
		ThresholdManifestSHA256: input.ThresholdManifestSHA256, CoordinatorPolicy: policy,
		SelectionKeySHA256: strings.Repeat("4", 64), ProjectionKeySHA256: strings.Repeat("5", 64),
		InferenceBudget: inferenceBudgetValue, EmbeddingBudget: embeddingBudgetValue,
		InferenceThreshold: input.Threshold, EmbeddingThreshold: input.Threshold,
	}
	profileSHA256, err := FrozenProfileSHA256(input.FrozenProfile)
	if err != nil {
		panic(err)
	}
	input.AblationProfileSHA256 = profileSHA256
	caseIDs := make([]string, len(input.Baseline))
	for index := range input.Baseline {
		caseIDs[index] = input.Baseline[index].CaseID
	}
	selectedSHA256, _, err := digestJSON(struct {
		ContractVersion string   `json:"contract_version"`
		CaseIDs         []string `json:"case_ids"`
	}{ContractVersion: ContractVersion, CaseIDs: caseIDs})
	if err != nil {
		panic(err)
	}
	caseSetSHA256, err := selectedCaseSetSHA256(input.DatasetSHA256, caseIDs)
	if err != nil {
		panic(err)
	}
	inferenceLedger, _ := NewLedger(InterventionInference, inferenceBudgetValue)
	embeddingLedger, _ := NewLedger(InterventionEmbedding, embeddingBudgetValue)
	inferenceUsage := inferenceLedger.Snapshot()
	embeddingUsage := embeddingLedger.Snapshot()
	if intervention == InterventionInference {
		inferenceUsage = usage
	} else {
		embeddingUsage = usage
	}
	baselineScoresSHA256, _ := scoresSHA256(input.Baseline)
	ablatedScoresSHA256, _ := scoresSHA256(input.Ablated)
	emptyScoresSHA256, _ := scoresSHA256(nil)
	input.Coordinator = CoordinationReport{
		ContractVersion: ContractVersion, BenchVersion: BenchVersionV9,
		ArtifactSHA256: testArtifactSHA, DatasetSHA256: testDatasetSHA,
		ThresholdManifestSHA256: testManifestSHA, AblationProfileSHA256: profileSHA256,
		CoordinatorPolicy: policy, SelectedCaseCount: len(caseIDs),
		SelectedCasesSHA256: selectedSHA256, SelectedCaseSetSHA256: caseSetSHA256,
		Ordinary: LaneReport{Lane: LaneOrdinary, Observation: ObservationAvailable, Complete: true,
			CompletedCases: len(caseIDs), ScoresSHA256: baselineScoresSHA256, Scores: input.Baseline},
		InferenceIntervention: LaneReport{Lane: LaneInference, Observation: ObservationUnavailable,
			UnavailableReason: UnavailableCaseFailure, ScoresSHA256: emptyScoresSHA256, SyntheticUsage: inferenceUsage},
		EmbeddingIntervention: LaneReport{Lane: LaneEmbedding, Observation: ObservationUnavailable,
			UnavailableReason: UnavailableCaseFailure, ScoresSHA256: emptyScoresSHA256, SyntheticUsage: embeddingUsage},
	}
	if intervention == InterventionInference {
		input.Coordinator.InferenceIntervention = LaneReport{
			Lane: LaneInference, Observation: ObservationChanged, Complete: true,
			CompletedCases: len(caseIDs), ScoresSHA256: ablatedScoresSHA256, Scores: input.Ablated, SyntheticUsage: usage,
		}
	} else {
		input.Coordinator.EmbeddingIntervention = LaneReport{
			Lane: LaneEmbedding, Observation: ObservationChanged, Complete: true,
			CompletedCases: len(caseIDs), ScoresSHA256: ablatedScoresSHA256, Scores: input.Ablated, SyntheticUsage: usage,
		}
	}
	input.Coordinator.CoordinatorSHA256, err = coordinatorDigest(input.Coordinator)
	if err != nil {
		panic(err)
	}
	return input
}

func rebindEvaluationPopulation(input *EvaluateInput) {
	input.FrozenProfile.CoordinatorPolicy.SampleSize = len(input.Baseline)
	input.FrozenProfile.CoordinatorPolicy.MaxRequests = len(input.Baseline) * 9
	input.Coordinator.CoordinatorPolicy = input.FrozenProfile.CoordinatorPolicy
	input.Coordinator.SelectedCaseCount = len(input.Baseline)
	input.Coordinator.Ordinary.Scores = input.Baseline
	input.Coordinator.Ordinary.ScoresSHA256, _ = scoresSHA256(input.Baseline)
	input.Coordinator.Ordinary.CompletedCases = len(input.Baseline)
	lane, err := input.Coordinator.lane(input.Intervention)
	if err != nil {
		panic(err)
	}
	lane.Scores = input.Ablated
	lane.ScoresSHA256, _ = scoresSHA256(input.Ablated)
	lane.CompletedCases = len(input.Ablated)
	if input.Intervention == InterventionInference {
		input.Coordinator.InferenceIntervention = lane
	} else {
		input.Coordinator.EmbeddingIntervention = lane
	}
	caseIDs := make([]string, len(input.Baseline))
	for index := range input.Baseline {
		caseIDs[index] = input.Baseline[index].CaseID
	}
	input.Coordinator.SelectedCasesSHA256, err = selectedCasesSHA256(caseIDs)
	if err != nil {
		panic(err)
	}
	input.Coordinator.SelectedCaseSetSHA256, err = selectedCaseSetSHA256(input.DatasetSHA256, caseIDs)
	if err != nil {
		panic(err)
	}
	input.AblationProfileSHA256, err = FrozenProfileSHA256(input.FrozenProfile)
	if err != nil {
		panic(err)
	}
	input.Coordinator.AblationProfileSHA256 = input.AblationProfileSHA256
	input.Coordinator.CoordinatorSHA256, err = coordinatorDigest(input.Coordinator)
	if err != nil {
		panic(err)
	}
}

func rebindEvaluationThreshold(input *EvaluateInput) {
	if input.Intervention == InterventionInference {
		input.FrozenProfile.InferenceThreshold = input.Threshold
	} else {
		input.FrozenProfile.EmbeddingThreshold = input.Threshold
	}
	var err error
	input.AblationProfileSHA256, err = FrozenProfileSHA256(input.FrozenProfile)
	if err != nil {
		panic(err)
	}
	input.Coordinator.AblationProfileSHA256 = input.AblationProfileSHA256
	input.Coordinator.CoordinatorSHA256, err = coordinatorDigest(input.Coordinator)
	if err != nil {
		panic(err)
	}
}

func evaluateInputFromReport(
	t *testing.T,
	config CoordinatorConfig,
	report CoordinationReport,
	intervention Intervention,
	mode Mode,
) EvaluateInput {
	t.Helper()
	input := EvaluateInput{
		BenchVersion: BenchVersionV9, ArtifactSHA256: config.ArtifactSHA256,
		Intervention: intervention, Mode: mode, ProfileRevision: config.FrozenProfile.Revision,
		ThresholdManifestSHA256: config.FrozenProfile.ThresholdManifestSHA256,
		DatasetSHA256:           config.FrozenProfile.DatasetSHA256, FrozenProfile: config.FrozenProfile,
		AblationProfileSHA256: config.ProfileSHA256, Coordinator: report,
	}
	input.Threshold = config.FrozenProfile.InferenceThreshold
	if intervention == InterventionEmbedding {
		input.Threshold = config.FrozenProfile.EmbeddingThreshold
	}
	if population, err := report.GatePopulation(intervention); err == nil {
		input.Baseline = population.Baseline
		input.Ablated = population.Ablated
	}
	return input
}

func TestInterventionParsingAndVersionGate(t *testing.T) {
	t.Parallel()
	for _, testCase := range []struct {
		raw  string
		want Intervention
		err  bool
	}{
		{raw: "", want: InterventionNone},
		{raw: "none", want: InterventionNone},
		{raw: "inference", want: InterventionInference},
		{raw: "embedding", want: InterventionEmbedding},
		{raw: " inference", err: true},
		{raw: "chat", err: true},
	} {
		got, err := ParseIntervention(testCase.raw)
		if (err != nil) != testCase.err || got != testCase.want {
			t.Errorf("ParseIntervention(%q) = (%q, %v), want (%q, err=%v)", testCase.raw, got, err, testCase.want, testCase.err)
		}
	}
	if err := InterventionNone.ValidateFor(8, false); err != nil {
		t.Fatalf("ordinary omitted intervention rejected: %v", err)
	}
	for _, testCase := range []struct {
		version      int
		confirmation bool
		wantError    bool
	}{
		{version: 9, confirmation: true},
		{version: 8, confirmation: true, wantError: true},
		{version: 9, confirmation: false, wantError: true},
		{version: 10, confirmation: true, wantError: true},
		{version: 11, confirmation: false, wantError: true},
		// v12+ counterfactual slice runs on the scored path (no confirmation).
		{version: 12, confirmation: false},
		{version: 12, confirmation: true},
		{version: 13, confirmation: false},
	} {
		err := InterventionInference.ValidateFor(testCase.version, testCase.confirmation)
		if (err != nil) != testCase.wantError {
			t.Errorf("ValidateFor(%d, %v) error=%v, wantError=%v", testCase.version, testCase.confirmation, err, testCase.wantError)
		}
	}
	if err := Intervention("future").ValidateFor(9, true); err == nil {
		t.Fatal("unknown intervention accepted")
	}
}

func TestNewSeededResponderIsDeterministic(t *testing.T) {
	budget := Budget{MaxChatRequests: 8, MaxChatInputBytes: 1 << 16}
	seedA := sha256.Sum256([]byte("case-a"))
	seedB := sha256.Sum256([]byte("case-b"))

	first, err := NewSeededResponder(InterventionInference, budget, seedA[:])
	if err != nil {
		t.Fatalf("NewSeededResponder(a) error = %v", err)
	}
	second, err := NewSeededResponder(InterventionInference, budget, seedA[:])
	if err != nil {
		t.Fatalf("NewSeededResponder(a') error = %v", err)
	}
	other, err := NewSeededResponder(InterventionInference, budget, seedB[:])
	if err != nil {
		t.Fatalf("NewSeededResponder(b) error = %v", err)
	}

	// Same seed -> byte-identical synthesized completion content (the answer the
	// harness would read), so a re-run draws the same perturbation.
	a1, err := first.Chat("test-model", 128)
	if err != nil {
		t.Fatalf("first.Chat error = %v", err)
	}
	a2, err := second.Chat("test-model", 128)
	if err != nil {
		t.Fatalf("second.Chat error = %v", err)
	}
	if a1.Choices[0].Message.Content != a2.Choices[0].Message.Content {
		t.Fatalf("same seed produced different completions:\n%q\n%q",
			a1.Choices[0].Message.Content, a2.Choices[0].Message.Content)
	}
	// A different seed must (with overwhelming probability) perturb differently.
	b1, err := other.Chat("test-model", 128)
	if err != nil {
		t.Fatalf("other.Chat error = %v", err)
	}
	if a1.Choices[0].Message.Content == b1.Choices[0].Message.Content {
		t.Fatalf("distinct seeds collided on completion content: %q", a1.Choices[0].Message.Content)
	}

	// A short seed is rejected.
	if _, err := NewSeededResponder(InterventionInference, budget, []byte("short")); err == nil {
		t.Fatal("short seed accepted")
	}
}

func TestModeParsingIsStrict(t *testing.T) {
	t.Parallel()
	for _, mode := range []Mode{ModeOff, ModeShadow, ModeEnforce} {
		got, err := ParseMode(string(mode))
		if err != nil || got != mode {
			t.Errorf("ParseMode(%q) = (%q, %v)", mode, got, err)
		}
	}
	for _, invalid := range []string{"", "observe", "shadow ", "ENFORCE"} {
		if _, err := ParseMode(invalid); err == nil {
			t.Errorf("invalid mode %q accepted", invalid)
		}
	}
}

func TestBudgetsAreFiniteAndLaneSpecific(t *testing.T) {
	t.Parallel()
	valid := []struct {
		intervention Intervention
		budget       Budget
	}{
		{InterventionInference, inferenceBudget(1, 1)},
		{InterventionInference, inferenceBudget(maximumSyntheticRequests, maximumSyntheticInputBytes)},
		{InterventionEmbedding, embeddingBudget(1, 1, 1)},
		{InterventionEmbedding, embeddingBudget(maximumSyntheticRequests, maximumSyntheticEmbeddingInputs, maximumSyntheticInputBytes)},
	}
	for _, testCase := range valid {
		if _, err := NewLedger(testCase.intervention, testCase.budget); err != nil {
			t.Errorf("valid %s budget rejected: %v", testCase.intervention, err)
		}
	}
	invalid := []struct {
		name         string
		intervention Intervention
		budget       Budget
	}{
		{"none", InterventionNone, Budget{}},
		{"inference zero requests", InterventionInference, inferenceBudget(0, 1)},
		{"inference zero bytes", InterventionInference, inferenceBudget(1, 0)},
		{"inference request hard max", InterventionInference, inferenceBudget(maximumSyntheticRequests+1, 1)},
		{"inference byte hard max", InterventionInference, inferenceBudget(1, maximumSyntheticInputBytes+1)},
		{"inference cross lane", InterventionInference, Budget{MaxChatRequests: 1, MaxChatInputBytes: 1, MaxEmbeddingRequests: 1}},
		{"embedding zero requests", InterventionEmbedding, embeddingBudget(0, 1, 1)},
		{"embedding zero inputs", InterventionEmbedding, embeddingBudget(1, 0, 1)},
		{"embedding zero bytes", InterventionEmbedding, embeddingBudget(1, 1, 0)},
		{"embedding request hard max", InterventionEmbedding, embeddingBudget(maximumSyntheticRequests+1, 1, 1)},
		{"embedding input hard max", InterventionEmbedding, embeddingBudget(1, maximumSyntheticEmbeddingInputs+1, 1)},
		{"embedding byte hard max", InterventionEmbedding, embeddingBudget(1, 1, maximumSyntheticInputBytes+1)},
		{"embedding cross lane", InterventionEmbedding, Budget{MaxEmbeddingRequests: 1, MaxEmbeddingInputs: 1, MaxEmbeddingInputBytes: 1, MaxChatRequests: 1}},
	}
	for _, testCase := range invalid {
		t.Run(testCase.name, func(t *testing.T) {
			t.Parallel()
			if _, err := NewLedger(testCase.intervention, testCase.budget); err == nil {
				t.Fatal("invalid budget accepted")
			}
		})
	}
}

func boundResponder(t *testing.T, intervention Intervention, budget Budget, projectionKey []byte) *Responder {
	t.Helper()
	responder, err := NewResponder(intervention, budget)
	if err != nil {
		t.Fatal(err)
	}
	responder.bindProjection(projectionKey, testArtifactSHA, intervention)
	responder.now = func() time.Time { return testProjectionNow }
	return responder
}

func TestKeyedCreatedUsesARecentPastWindowWithoutAnAbsoluteEpoch(t *testing.T) {
	t.Parallel()
	seed := projectedDigest(bytes.Repeat([]byte{0x51}, sha256.Size), "created", 17, []byte("payload"))
	created := keyedCreated(seed, testProjectionNow)
	oldest := testProjectionNow.Add(-projectedCreatedLookback).Unix()
	if created < oldest || created >= testProjectionNow.Unix() {
		t.Fatalf("created=%d outside recent-past window [%d,%d)", created, oldest, testProjectionNow.Unix())
	}
	if replayed := keyedCreated(seed, testProjectionNow); replayed != created {
		t.Fatalf("same seed and clock did not replay: first=%d replayed=%d", created, replayed)
	}

	// Advancing the response clock advances the same keyed projection by the
	// same amount. This rejects a regression to another fixed calendar range.
	advancedNow := testProjectionNow.Add(37 * time.Minute)
	advanced := keyedCreated(seed, advancedNow)
	if advanced-created != int64((37*time.Minute)/time.Second) {
		t.Fatalf("created timestamp retained an absolute epoch: first=%d advanced=%d", created, advanced)
	}
}

func TestKeyedCreatedAdversarialKeysAndCallsRemainPastAndDistributed(t *testing.T) {
	t.Parallel()
	const callsPerKey = 4096
	keys := [][]byte{
		bytes.Repeat([]byte{0x00}, sha256.Size),
		bytes.Repeat([]byte{0xff}, sha256.Size),
		[]byte("reviewer-controlled-projection-key"),
	}
	windowSeconds := int(projectedCreatedLookback / time.Second)
	timelines := make([][]int64, 0, len(keys))
	for keyIndex, key := range keys {
		counts := make([]int, windowSeconds)
		timeline := make([]int64, 0, callsPerKey)
		for call := uint64(1); call <= callsPerKey; call++ {
			seed := projectedDigest(key, "adversarial-created", call, []byte("same request"))
			created := keyedCreated(seed, testProjectionNow)
			lag := testProjectionNow.Unix() - created
			if lag < 1 || lag > int64(windowSeconds) {
				t.Fatalf("key=%d call=%d produced future/stale lag=%d", keyIndex, call, lag)
			}
			if replayed := keyedCreated(seed, testProjectionNow); replayed != created {
				t.Fatalf("key=%d call=%d was nondeterministic", keyIndex, call)
			}
			counts[lag-1]++
			timeline = append(timeline, created)
		}

		occupied := 0
		maxBucket := 0
		for _, count := range counts {
			if count > 0 {
				occupied++
			}
			maxBucket = max(maxBucket, count)
		}
		if occupied < windowSeconds*9/10 {
			t.Fatalf("key=%d occupied only %d/%d recent seconds", keyIndex, occupied, windowSeconds)
		}
		if maxBucket > callsPerKey/20 {
			t.Fatalf("key=%d concentrated %d/%d calls in one timestamp", keyIndex, maxBucket, callsPerKey)
		}
		timelines = append(timelines, timeline)
	}
	for index := 1; index < len(timelines); index++ {
		if reflect.DeepEqual(timelines[0], timelines[index]) {
			t.Fatalf("projection keys %d and 0 produced the same created timeline", index)
		}
	}
}

func FuzzKeyedCreatedIsDeterministicRecentAndNeverFuture(f *testing.F) {
	f.Add([]byte("ordinary key material"), uint32(0))
	f.Add(bytes.Repeat([]byte{0xff}, 64), ^uint32(0))
	f.Add([]byte{}, uint32(86_400))
	f.Fuzz(func(t *testing.T, material []byte, clockOffset uint32) {
		seed := sha256.Sum256(material)
		now := time.Date(2000, time.January, 1, 0, 0, 0, 0, time.UTC).
			Add(time.Duration(clockOffset) * time.Second)
		first := keyedCreated(seed[:], now)
		second := keyedCreated(seed[:], now)
		if first != second {
			t.Fatalf("same key and clock changed created: first=%d second=%d", first, second)
		}
		lag := now.Unix() - first
		if lag < 1 || lag > int64(projectedCreatedLookback/time.Second) {
			t.Fatalf("created=%d now=%d lag=%d outside recent-past window", first, now.Unix(), lag)
		}
	})
}

func TestSyntheticChatCompletionIsKeyedProviderShapedAndReplayable(t *testing.T) {
	t.Parallel()
	key := []byte("projection-key-32-bytes-long-fixed")
	firstResponder := boundResponder(t, InterventionInference, inferenceBudget(4, 256), key)
	secondResponder := boundResponder(t, InterventionInference, inferenceBudget(4, 256), key)
	first, err := firstResponder.Chat("openai/gpt-oss-20b", 48)
	if err != nil {
		t.Fatal(err)
	}
	second, err := firstResponder.Chat("openai/gpt-oss-20b", 48)
	if err != nil {
		t.Fatal(err)
	}
	replayedFirst, err := secondResponder.Chat("openai/gpt-oss-20b", 48)
	if err != nil {
		t.Fatal(err)
	}
	replayedSecond, err := secondResponder.Chat("openai/gpt-oss-20b", 48)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(first, replayedFirst) || !reflect.DeepEqual(second, replayedSecond) {
		t.Fatalf("bound chat sequence did not replay deterministically:\n%+v\n%+v", first, replayedFirst)
	}
	firstJSON, err := json.Marshal(first)
	if err != nil {
		t.Fatal(err)
	}
	secondJSON, _ := json.Marshal(second)
	if bytes.Equal(firstJSON, secondJSON) || first.ID == second.ID || first.Choices[0].Message.Content == second.Choices[0].Message.Content {
		t.Fatalf("synthetic chat reused a fingerprint:\n%s\n%s", firstJSON, secondJSON)
	}
	var envelope map[string]any
	if err := json.Unmarshal(firstJSON, &envelope); err != nil {
		t.Fatal(err)
	}
	for _, field := range []string{"id", "object", "created", "model", "choices", "usage"} {
		if _, found := envelope[field]; !found {
			t.Errorf("provider envelope missing %q", field)
		}
	}
	if bytes.Contains(firstJSON, []byte("synthetic")) || bytes.Contains(firstJSON, []byte("ablation\"")) {
		t.Fatalf("harness-visible response exposes intervention marker: %s", firstJSON)
	}
	if len(first.Choices) != 1 || first.Choices[0].Message.Role != "assistant" ||
		first.Choices[0].Message.Content == "" || first.Choices[0].FinishReason != "stop" {
		t.Fatalf("invalid chat choice: %+v", first.Choices)
	}
	oldestCreated := testProjectionNow.Add(-projectedCreatedLookback).Unix()
	if first.Created < oldestCreated || first.Created >= testProjectionNow.Unix() ||
		first.Usage.PromptTokens <= 0 || first.Usage.CompletionTokens <= 0 ||
		first.Usage.TotalTokens != first.Usage.PromptTokens+first.Usage.CompletionTokens {
		t.Fatalf("provider usage is implausible or inconsistent: %+v", first)
	}
	different := boundResponder(t, InterventionInference, inferenceBudget(2, 128), []byte("different-projection-key-32-bytes!!"))
	differentResponse, err := different.Chat("openai/gpt-oss-20b", 48)
	if err != nil {
		t.Fatal(err)
	}
	if differentResponse.ID == first.ID || differentResponse.Choices[0].Message.Content == first.Choices[0].Message.Content {
		t.Fatal("different projection keys reused a chat marker")
	}
}

func TestLearnedExactChatMarkersDoNotReplayAcrossProjectionKeys(t *testing.T) {
	t.Parallel()
	training := boundResponder(
		t, InterventionInference, inferenceBudget(64, 64*128),
		[]byte("training-projection-key-32-bytes--"),
	)
	unseen := boundResponder(
		t, InterventionInference, inferenceBudget(64, 64*128),
		[]byte("unseen-projection-key-32-bytes----"),
	)
	known := make(map[string]struct{}, 64)
	for index := range 64 {
		response, err := training.Chat("model", uint64(32+index))
		if err != nil {
			t.Fatal(err)
		}
		known[response.Choices[0].Message.Content] = struct{}{}
	}
	if len(known) < 56 {
		t.Fatalf("keyed grammar produced only %d distinct training responses", len(known))
	}
	for index := range 64 {
		response, err := unseen.Chat("model", uint64(32+index))
		if err != nil {
			t.Fatal(err)
		}
		if _, learnedMarker := known[response.Choices[0].Message.Content]; learnedMarker {
			t.Fatalf("unseen projection reused learned exact marker at call %d", index)
		}
	}
}

func TestSyntheticEmbeddingIsKeyedNormalizedDeterministicAndIndependent(t *testing.T) {
	t.Parallel()
	key := bytes.Repeat([]byte{0x42}, sha256.Size)
	response, err := syntheticEmbeddingResponse([]string{"same   semantic\ttext", " same semantic text\r\n", "different text"}, key)
	if err != nil {
		t.Fatal(err)
	}
	if len(response.Embeddings) != 3 || response.PromptEvalCount <= 0 {
		t.Fatalf("response shape = %+v", response)
	}
	for index, vector := range response.Embeddings {
		if len(vector) != EmbeddingDimensions {
			t.Fatalf("vector %d dimensions=%d", index, len(vector))
		}
		norm := 0.0
		for _, value := range vector {
			if value == 0 || math.IsNaN(value) || math.IsInf(value, 0) {
				t.Fatalf("vector %d contains invalid value %v", index, value)
			}
			norm += value * value
		}
		if math.Abs(norm-1) > 1e-12 {
			t.Fatalf("vector %d squared norm=%0.16f, want 1", index, norm)
		}
	}
	if !reflect.DeepEqual(response.Embeddings[0], response.Embeddings[1]) {
		t.Fatal("identical normalized inputs produced different embeddings")
	}
	if reflect.DeepEqual(response.Embeddings[0], response.Embeddings[2]) {
		t.Fatal("different normalized inputs produced an identical embedding")
	}
	reordered, err := syntheticEmbeddingResponse([]string{"different text", "same semantic text"}, key)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(reordered.Embeddings[0], response.Embeddings[2]) ||
		!reflect.DeepEqual(reordered.Embeddings[1], response.Embeddings[0]) {
		t.Fatal("embedding projection depends on batch order")
	}
	otherKey := bytes.Repeat([]byte{0x24}, sha256.Size)
	other, err := syntheticEmbeddingResponse([]string{"same semantic text"}, otherKey)
	if err != nil {
		t.Fatal(err)
	}
	if reflect.DeepEqual(other.Embeddings[0], response.Embeddings[0]) {
		t.Fatal("embedding projection ignored its bound key")
	}
	response.Embeddings[0][0] = 99
	if response.Embeddings[1][0] == 99 {
		t.Fatal("synthetic vectors share mutable storage")
	}
	if _, err := syntheticEmbeddingResponse(nil, key); err == nil {
		t.Fatal("zero input count accepted")
	}
	if _, err := syntheticEmbeddingResponse([]string{"value"}, []byte("short")); err == nil {
		t.Fatal("short projection key accepted")
	}
}

func TestDeterministicEmbeddingReplayStillSpendsEveryBoundedAdmission(t *testing.T) {
	t.Parallel()
	responder := boundResponder(
		t, InterventionEmbedding, embeddingBudget(2, 2, 128),
		[]byte("projection-key-32-bytes-long-fixed"),
	)
	first, err := responder.Embeddings([]string{" repeated\tinput "})
	if err != nil {
		t.Fatal(err)
	}
	second, err := responder.Embeddings([]string{"repeated input"})
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(first.Embeddings, second.Embeddings) {
		t.Fatal("identical normalized input did not replay deterministically")
	}
	if _, err := responder.Embeddings([]string{"repeated input"}); !errors.Is(err, ErrBudgetExhausted) {
		t.Fatalf("replayed input bypassed request budget: %v", err)
	}
	usage := responder.Snapshot()
	if usage.EmbeddingAttempts != 3 || usage.EmbeddingApplied != 2 ||
		usage.EmbeddingInputs != 2 || usage.RejectedRequests != 1 || !usage.BudgetExhausted {
		t.Fatalf("replay accounting=%+v", usage)
	}
}

func TestResponderHasNoUpstreamCapableFields(t *testing.T) {
	t.Parallel()
	typeOfResponder := reflect.TypeOf(Responder{})
	for index := 0; index < typeOfResponder.NumField(); index++ {
		field := typeOfResponder.Field(index)
		if strings.Contains(strings.ToLower(field.Name), "client") || strings.Contains(strings.ToLower(field.Name), "url") {
			t.Fatalf("Responder gained upstream-capable field %s %s", field.Name, field.Type)
		}
	}
}

func TestResponderAccountsOnlyTheSelectedSyntheticLane(t *testing.T) {
	t.Parallel()
	inference, err := NewResponder(InterventionInference, inferenceBudget(2, 100))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := inference.Chat("openai/gpt-oss-20b", 12); err != nil {
		t.Fatal(err)
	}
	if _, err := inference.Embeddings([]string{"hello"}); !errors.Is(err, ErrWrongIntervention) {
		t.Fatalf("inference responder embedding error=%v", err)
	}
	inferenceUsage := inference.Snapshot()
	if !inferenceUsage.Synthetic || inferenceUsage.ChatAttempts != 1 || inferenceUsage.ChatApplied != 1 ||
		inferenceUsage.ChatInputBytes != 12 || inferenceUsage.EmbeddingAttempts != 0 ||
		inferenceUsage.UpstreamRequests != 0 || inferenceUsage.UpstreamInputTokens != 0 ||
		inferenceUsage.UpstreamOutputTokens != 0 || inferenceUsage.UpstreamProviderCostMicroUSD != 0 {
		t.Fatalf("inference usage=%+v", inferenceUsage)
	}

	embedding, err := NewResponder(InterventionEmbedding, embeddingBudget(2, 4, 100))
	if err != nil {
		t.Fatal(err)
	}
	got, err := embedding.Embeddings([]string{"semantic text", "unrelated words"})
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Embeddings) != 2 {
		t.Fatalf("embedding count=%d", len(got.Embeddings))
	}
	if _, err := embedding.Chat("openai/gpt-oss-20b", 12); !errors.Is(err, ErrWrongIntervention) {
		t.Fatalf("embedding responder chat error=%v", err)
	}
	embeddingUsage := embedding.Snapshot()
	if embeddingUsage.EmbeddingAttempts != 1 || embeddingUsage.EmbeddingApplied != 1 ||
		embeddingUsage.EmbeddingInputs != 2 || embeddingUsage.EmbeddingInputBytes != uint64(len("semantic text")+len("unrelated words")) ||
		embeddingUsage.ChatAttempts != 0 || embeddingUsage.UpstreamRequests != 0 {
		t.Fatalf("embedding usage=%+v", embeddingUsage)
	}
}

func TestResponderRejectsInvalidInputWithoutSpendingBudget(t *testing.T) {
	t.Parallel()
	inference, _ := NewResponder(InterventionInference, inferenceBudget(1, 10))
	if _, err := inference.Chat("", 1); err == nil {
		t.Fatal("empty model accepted")
	}
	if _, err := inference.Chat("model", 0); err == nil {
		t.Fatal("empty request accepted")
	}
	if got := inference.Snapshot(); got.ChatAttempts != 0 || got.ChatApplied != 0 {
		t.Fatalf("invalid chat spent budget: %+v", got)
	}
	embedding, _ := NewResponder(InterventionEmbedding, embeddingBudget(1, 2, 10))
	for _, inputs := range [][]string{nil, {}, {""}, {" \t\r\n"}, {"ok", ""}, {"ok", "  "}} {
		if _, err := embedding.Embeddings(inputs); err == nil {
			t.Errorf("invalid inputs %#v accepted", inputs)
		}
	}
	if got := embedding.Snapshot(); got.EmbeddingAttempts != 0 || got.EmbeddingApplied != 0 {
		t.Fatalf("invalid embeddings spent budget: %+v", got)
	}
}

func TestChatBudgetExhaustionIsStickyAndExplicit(t *testing.T) {
	t.Parallel()
	ledger := mustLedger(t, InterventionInference, inferenceBudget(2, 12))
	if err := ledger.AdmitChat(5); err != nil {
		t.Fatal(err)
	}
	if err := ledger.AdmitChat(7); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := ledger.AdmitChat(1); !errors.Is(err, ErrBudgetExhausted) {
			t.Fatalf("post-budget error=%v", err)
		}
	}
	got := ledger.Snapshot()
	if got.ChatAttempts != 4 || got.ChatApplied != 2 || got.ChatInputBytes != 12 ||
		got.RejectedRequests != 2 || !got.BudgetExhausted {
		t.Fatalf("usage=%+v", got)
	}
}

func TestEmbeddingBudgetsFailClosedIndependently(t *testing.T) {
	t.Parallel()
	for _, testCase := range []struct {
		name        string
		budget      Budget
		firstInputs uint64
		firstBytes  uint64
		nextInputs  uint64
		nextBytes   uint64
	}{
		{"requests", embeddingBudget(1, 10, 100), 1, 1, 1, 1},
		{"inputs", embeddingBudget(2, 2, 100), 2, 1, 1, 1},
		{"bytes", embeddingBudget(2, 10, 3), 1, 2, 1, 2},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			t.Parallel()
			ledger := mustLedger(t, InterventionEmbedding, testCase.budget)
			if err := ledger.AdmitEmbedding(testCase.firstInputs, testCase.firstBytes); err != nil {
				t.Fatal(err)
			}
			if err := ledger.AdmitEmbedding(testCase.nextInputs, testCase.nextBytes); !errors.Is(err, ErrBudgetExhausted) {
				t.Fatalf("exhaustion error=%v", err)
			}
			got := ledger.Snapshot()
			if got.EmbeddingAttempts != 2 || got.EmbeddingApplied != 1 || got.RejectedRequests != 1 || !got.BudgetExhausted {
				t.Fatalf("usage=%+v", got)
			}
		})
	}
}

func TestConcurrentChatAdmissionCannotExceedBudget(t *testing.T) {
	const (
		limit    = 64
		attempts = 1024
	)
	ledger := mustLedger(t, InterventionInference, inferenceBudget(limit, limit))
	var admitted atomic.Uint64
	var exhausted atomic.Uint64
	var wait sync.WaitGroup
	wait.Add(attempts)
	for range attempts {
		go func() {
			defer wait.Done()
			err := ledger.AdmitChat(1)
			switch {
			case err == nil:
				admitted.Add(1)
			case errors.Is(err, ErrBudgetExhausted):
				exhausted.Add(1)
			default:
				t.Errorf("unexpected admission error: %v", err)
			}
		}()
	}
	wait.Wait()
	got := ledger.Snapshot()
	if admitted.Load() != limit || exhausted.Load() != attempts-limit ||
		got.ChatApplied != limit || got.ChatInputBytes != limit || got.ChatAttempts != attempts ||
		got.RejectedRequests != attempts-limit || !got.BudgetExhausted {
		t.Fatalf("admitted=%d exhausted=%d usage=%+v", admitted.Load(), exhausted.Load(), got)
	}
}

func TestConcurrentPostExhaustionAccountingIsExact(t *testing.T) {
	const rejectedAttempts = 512
	ledger := mustLedger(t, InterventionInference, inferenceBudget(1, 1))
	if err := ledger.AdmitChat(1); err != nil {
		t.Fatal(err)
	}

	var rejected atomic.Uint64
	var wait sync.WaitGroup
	wait.Add(rejectedAttempts)
	for range rejectedAttempts {
		go func() {
			defer wait.Done()
			if err := ledger.AdmitChat(1); errors.Is(err, ErrBudgetExhausted) {
				rejected.Add(1)
			} else {
				t.Errorf("post-exhaustion admission error=%v", err)
			}
		}()
	}
	wait.Wait()

	got := ledger.Snapshot()
	if rejected.Load() != rejectedAttempts || got.ChatAttempts != rejectedAttempts+1 ||
		got.ChatApplied != 1 || got.ChatInputBytes != 1 || got.RejectedRequests != rejectedAttempts ||
		!got.BudgetExhausted || got.UpstreamRequests != 0 || got.UpstreamInputTokens != 0 ||
		got.UpstreamOutputTokens != 0 || got.UpstreamProviderCostMicroUSD != 0 {
		t.Fatalf("rejected=%d usage=%+v", rejected.Load(), got)
	}
}

func TestConcurrentEmbeddingAdmissionCannotExceedAnyBudget(t *testing.T) {
	const (
		limit    = 40
		attempts = 500
	)
	ledger := mustLedger(t, InterventionEmbedding, embeddingBudget(limit, limit*2, limit*4))
	var admitted atomic.Uint64
	var wait sync.WaitGroup
	wait.Add(attempts)
	for range attempts {
		go func() {
			defer wait.Done()
			if err := ledger.AdmitEmbedding(2, 4); err == nil {
				admitted.Add(1)
			} else if !errors.Is(err, ErrBudgetExhausted) {
				t.Errorf("unexpected admission error: %v", err)
			}
		}()
	}
	wait.Wait()
	got := ledger.Snapshot()
	if admitted.Load() != limit || got.EmbeddingApplied != limit || got.EmbeddingInputs != limit*2 ||
		got.EmbeddingInputBytes != limit*4 || got.EmbeddingAttempts != attempts ||
		got.RejectedRequests != attempts-limit || !got.BudgetExhausted {
		t.Fatalf("admitted=%d usage=%+v", admitted.Load(), got)
	}
}

func TestConcurrentSnapshotsRemainCoherent(t *testing.T) {
	const limit = 250
	ledger := mustLedger(t, InterventionInference, inferenceBudget(limit, limit))
	var wait sync.WaitGroup
	wait.Add(2)
	go func() {
		defer wait.Done()
		for range limit {
			if err := ledger.AdmitChat(1); err != nil {
				t.Errorf("admission: %v", err)
				return
			}
		}
	}()
	go func() {
		defer wait.Done()
		for range limit * 2 {
			snapshot := ledger.Snapshot()
			if snapshot.ChatApplied > snapshot.ChatAttempts || snapshot.ChatInputBytes != snapshot.ChatApplied {
				t.Errorf("incoherent snapshot: %+v", snapshot)
				return
			}
		}
	}()
	wait.Wait()
}

func TestGateStatusThresholdsAndFactors(t *testing.T) {
	for _, testCase := range []struct {
		name         string
		mode         Mode
		threshold    float64
		calls        int
		mutate       func(*EvaluateInput)
		wantStatus   Status
		wantReason   Reason
		wantSemantic float64
		wantApplied  float64
	}{
		{"enforce fails closed on observed drop", ModeEnforce, 0.5, 6, nil, StatusUnavailable, ReasonEnforceProofUnavailable, 0, 0},
		{"enforce fails closed at threshold", ModeEnforce, 0.5, 6, nil, StatusUnavailable, ReasonEnforceProofUnavailable, 0, 0},
		{"enforce fails closed below threshold", ModeEnforce, 0.500001, 6, nil, StatusUnavailable, ReasonEnforceProofUnavailable, 0, 0},
		{"shadow drop is a completed non-causal observation", ModeShadow, 0.2, 6, nil, StatusFailed, ReasonObservationalDropNotCausal, 0, 1},
		{"shadow threshold boundary is a completed non-causal observation", ModeShadow, 0.5, 6, nil, StatusFailed, ReasonObservationalDropNotCausal, 0, 1},
		{"shadow failure neutral", ModeShadow, 0.500001, 6, nil, StatusFailed, ReasonDeltaBelowThreshold, 0, 1},
		{"negative delta remains shadow failure", ModeShadow, 0.1, 6, func(input *EvaluateInput) {
			input.Baseline = []CaseScore{{CaseID: "a", Score: 0.1}}
			input.Ablated = []CaseScore{{CaseID: "a", Score: 0.9}}
			rebindEvaluationPopulation(input)
		}, StatusFailed, ReasonDeltaBelowThreshold, 0, 1},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			usage := usageWithCalls(t, InterventionInference, testCase.calls)
			input := evaluationInput(InterventionInference, testCase.mode, usage)
			input.Threshold = testCase.threshold
			rebindEvaluationThreshold(&input)
			if testCase.mutate != nil {
				testCase.mutate(&input)
			}
			got, err := Evaluate(input)
			if err != nil {
				t.Fatal(err)
			}
			if got.Evidence.Status != testCase.wantStatus || got.Evidence.Reason != testCase.wantReason ||
				got.Evidence.SemanticFactor != testCase.wantSemantic || got.Evidence.AppliedFactor != testCase.wantApplied {
				t.Fatalf("status=%s reason=%s semantic=%v applied=%v, want %s %s %v/%v",
					got.Evidence.Status, got.Evidence.Reason, got.Evidence.SemanticFactor, got.Evidence.AppliedFactor,
					testCase.wantStatus, testCase.wantReason, testCase.wantSemantic, testCase.wantApplied)
			}
		})
	}
}

func TestGateThresholdDecisionUsesUnroundedMeansAndDelta(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name      string
		baseline  float64
		ablated   float64
		threshold float64
		want      Status
	}{
		{
			name: "raw delta is ambiguous although separately rounded means would fail",
			// Raw delta is 0.50000098. The published means are 0.8 and 0.3,
			// whose subtraction is only 0.5.
			baseline: 0.80000049, ablated: 0.29999951, threshold: 0.5000005, want: StatusFailed,
		},
		{
			name: "raw delta fails although published delta and threshold are equal",
			// Both values publish as 0.5, but 0.5000001 < 0.5000004.
			baseline: 0.8000001, ablated: 0.3, threshold: 0.5000004, want: StatusFailed,
		},
	}
	for _, testCase := range tests {
		t.Run(testCase.name, func(t *testing.T) {
			input := evaluationInput(InterventionInference, ModeShadow, usageWithCalls(t, InterventionInference, 2))
			input.Baseline = []CaseScore{{CaseID: "boundary", Score: testCase.baseline}}
			input.Ablated = []CaseScore{{CaseID: "boundary", Score: testCase.ablated}}
			rebindEvaluationPopulation(&input)
			input.Threshold = testCase.threshold
			rebindEvaluationThreshold(&input)
			got, err := Evaluate(input)
			if err != nil {
				t.Fatal(err)
			}
			if got.Evidence.Status != testCase.want {
				t.Fatalf("status=%s mean=%v ablated=%v delta=%v threshold=%v, want %s",
					got.Evidence.Status, got.Evidence.BaselineMean, got.Evidence.AblatedMean,
					got.Evidence.Delta, got.Evidence.Threshold, testCase.want)
			}
		})
	}
}

func TestFlatAnswerMachineFailsAfterCallingIntervention(t *testing.T) {
	usage := usageWithCalls(t, InterventionEmbedding, 4)
	input := evaluationInput(InterventionEmbedding, ModeShadow, usage)
	input.Baseline = []CaseScore{{CaseID: "a", Score: 0.8}, {CaseID: "b", Score: 0.4}}
	input.Ablated = []CaseScore{{CaseID: "a", Score: 0.8}, {CaseID: "b", Score: 0.4}}
	rebindEvaluationPopulation(&input)
	input.Threshold = 0.01
	rebindEvaluationThreshold(&input)
	got, err := Evaluate(input)
	if err != nil {
		t.Fatal(err)
	}
	if got.Evidence.Delta != 0 || got.Evidence.Status != StatusFailed || got.Evidence.Reason != ReasonDeltaBelowThreshold ||
		got.Evidence.SemanticFactor != 0 || got.Evidence.AppliedFactor != 1 {
		t.Fatalf("flat gate=%+v", got.Evidence)
	}
}

func TestBudgetExhaustionIsUnavailableNotInvariance(t *testing.T) {
	config := coordinatorConfig(2)
	config.FrozenProfile.InferenceBudget = inferenceBudget(1, 32)
	config.ProfileSHA256, _ = FrozenProfileSHA256(config.FrozenProfile)
	coordinator := newCoordinator(t, config)
	inference, err := NewResponder(InterventionInference, config.FrozenProfile.InferenceBudget)
	if err != nil {
		t.Fatal(err)
	}
	embedding, err := NewResponder(InterventionEmbedding, config.FrozenProfile.EmbeddingBudget)
	if err != nil {
		t.Fatal(err)
	}
	runner := caseRunnerFunc(func(_ context.Context, request RunRequest) (CaseRunResult, error) {
		if request.Lane == LaneInference {
			if _, err := request.Responder.Chat("model", 8); err != nil {
				return CaseRunResult{}, err
			}
			_, err := request.Responder.Chat("model", 8)
			return CaseRunResult{Score: 0.2}, err
		}
		if err := useSynthetic(request); err != nil {
			return CaseRunResult{}, err
		}
		return CaseRunResult{Score: 0.8}, nil
	})
	report, err := coordinator.Coordinate(context.Background(), eligiblePopulation(4), runner, inference, embedding)
	if err != nil {
		t.Fatal(err)
	}
	input := evaluateInputFromReport(t, config, report, InterventionInference, ModeEnforce)
	got, err := Evaluate(input)
	if err != nil {
		t.Fatal(err)
	}
	if got.Evidence.Status != StatusUnavailable || got.Evidence.Reason != ReasonBudgetExhausted ||
		got.Evidence.SemanticFactor != 0 || got.Evidence.AppliedFactor != 0 {
		t.Fatalf("budget gate=%+v", got.Evidence)
	}
}

func TestRealCoordinatorUnavailableEvidenceForZeroAndProbeOnlyCalls(t *testing.T) {
	for _, testCase := range []struct {
		name       string
		calls      int
		wantReason Reason
	}{
		{name: "zero calls", calls: 0, wantReason: ReasonNoRelevantCalls},
		{name: "one probe", calls: 1, wantReason: ReasonInsufficientCalls},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			config := coordinatorConfig(2)
			coordinator := newCoordinator(t, config)
			inference, embedding := responders(t, 2)
			runner := caseRunnerFunc(func(_ context.Context, request RunRequest) (CaseRunResult, error) {
				if request.Lane == LaneInference {
					for range testCase.calls {
						if _, err := request.Responder.Chat("model", 8); err != nil {
							return CaseRunResult{}, err
						}
					}
					return CaseRunResult{Score: 0}, nil
				}
				if err := useSynthetic(request); err != nil {
					return CaseRunResult{}, err
				}
				return CaseRunResult{Score: 0.9}, nil
			})
			report, err := coordinator.Coordinate(context.Background(), eligiblePopulation(4), runner, inference, embedding)
			if err != nil {
				t.Fatal(err)
			}
			got, err := Evaluate(evaluateInputFromReport(t, config, report, InterventionInference, ModeEnforce))
			if err != nil {
				t.Fatal(err)
			}
			if got.Evidence.Status != StatusUnavailable || got.Evidence.Reason != testCase.wantReason ||
				got.Evidence.SampleCount != 0 || got.Evidence.SemanticFactor != 0 || got.Evidence.AppliedFactor != 0 {
				t.Fatalf("evidence=%+v", got.Evidence)
			}
		})
	}
}

func TestCoordinatorScoreCommitmentsSurviveJSONAndRejectTampering(t *testing.T) {
	config := coordinatorConfig(2)
	coordinator := newCoordinator(t, config)
	inference, embedding := responders(t, 2)
	runner := caseRunnerFunc(func(_ context.Context, request RunRequest) (CaseRunResult, error) {
		if err := useSynthetic(request); err != nil {
			return CaseRunResult{}, err
		}
		if request.Lane == LaneInference {
			return CaseRunResult{Score: 0.2}, nil
		}
		return CaseRunResult{Score: 0.9}, nil
	})
	report, err := coordinator.Coordinate(context.Background(), eligiblePopulation(4), runner, inference, embedding)
	if err != nil {
		t.Fatal(err)
	}
	population, err := report.GatePopulation(InterventionInference)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	var persisted CoordinationReport
	if err := json.Unmarshal(raw, &persisted); err != nil {
		t.Fatal(err)
	}
	input := evaluateInputFromReport(t, config, persisted, InterventionInference, ModeShadow)
	input.Baseline = population.Baseline
	input.Ablated = population.Ablated
	if _, err := Evaluate(input); err != nil {
		t.Fatalf("persisted score commitments did not validate: %v", err)
	}
	input.Baseline[0].Score -= 0.1
	if _, err := Evaluate(input); err == nil {
		t.Fatal("tampered persisted baseline was accepted")
	}
}

func TestLegacyResponseFingerprintSabotageCannotQualify(t *testing.T) {
	config := coordinatorConfig(2)
	coordinator := newCoordinator(t, config)
	inference, embedding := responders(t, 2)
	runner := caseRunnerFunc(func(_ context.Context, request RunRequest) (CaseRunResult, error) {
		legacyMarker := false
		switch request.Lane {
		case LaneInference:
			for range minimumRelevantCallsPerCase {
				response, err := request.Responder.Chat("model", 32)
				if err != nil {
					return CaseRunResult{}, err
				}
				legacyMarker = legacyMarker || response.ID == "chatcmpl-000000000000000000000000" ||
					response.Created == 0 || response.Usage.TotalTokens == 0 ||
					response.Choices[0].Message.Content == "A neutral placeholder response."
			}
		case LaneEmbedding:
			for range minimumRelevantCallsPerCase {
				response, err := request.Responder.Embeddings([]string{"probe"})
				if err != nil {
					return CaseRunResult{}, err
				}
				constant := 1 / math.Sqrt(EmbeddingDimensions)
				legacyMarker = legacyMarker || response.Embeddings[0][0] == constant
			}
		}
		if legacyMarker {
			return CaseRunResult{Score: 0}, nil
		}
		return CaseRunResult{Score: 0.9}, nil
	})
	report, err := coordinator.Coordinate(context.Background(), eligiblePopulation(4), runner, inference, embedding)
	if err != nil {
		t.Fatal(err)
	}
	for _, intervention := range []Intervention{InterventionInference, InterventionEmbedding} {
		got, err := Evaluate(evaluateInputFromReport(t, config, report, intervention, ModeShadow))
		if err != nil {
			t.Fatal(err)
		}
		if got.Evidence.Status != StatusFailed || got.Evidence.Reason != ReasonDeltaBelowThreshold {
			t.Fatalf("legacy marker sabotage qualified %s: %+v", intervention, got.Evidence)
		}
		got, err = Evaluate(evaluateInputFromReport(t, config, report, intervention, ModeEnforce))
		if err != nil {
			t.Fatal(err)
		}
		if got.Evidence.Status != StatusUnavailable || got.Evidence.Reason != ReasonEnforceProofUnavailable {
			t.Fatalf("v1 enforce did not fail closed for %s: %+v", intervention, got.Evidence)
		}
	}
}

func TestAdaptiveTreatmentDetectionIsContainedToShadow(t *testing.T) {
	config := coordinatorConfig(2)
	coordinator := newCoordinator(t, config)
	inference, embedding := responders(t, 2)
	runner := caseRunnerFunc(func(_ context.Context, request RunRequest) (CaseRunResult, error) {
		score := 0.9
		switch request.Lane {
		case LaneInference:
			for range minimumRelevantCallsPerCase {
				if _, err := request.Responder.Chat("model", 48); err != nil {
					return CaseRunResult{}, err
				}
			}
			score = 0 // Adaptive sabotage after recognizing the treated capability.
		case LaneEmbedding:
			for range minimumRelevantCallsPerCase {
				if _, err := request.Responder.Embeddings([]string{"adaptive probe"}); err != nil {
					return CaseRunResult{}, err
				}
			}
			score = 0
		}
		return CaseRunResult{Score: score}, nil
	})
	report, err := coordinator.Coordinate(context.Background(), eligiblePopulation(4), runner, inference, embedding)
	if err != nil {
		t.Fatal(err)
	}
	for _, intervention := range []Intervention{InterventionInference, InterventionEmbedding} {
		shadow, err := Evaluate(evaluateInputFromReport(t, config, report, intervention, ModeShadow))
		if err != nil {
			t.Fatal(err)
		}
		if shadow.Evidence.Status != StatusFailed || shadow.Evidence.Reason != ReasonObservationalDropNotCausal ||
			shadow.Evidence.SemanticFactor != 0 || shadow.Evidence.AppliedFactor != 1 {
			t.Fatalf("adaptive shadow treatment was promoted for %s: %+v", intervention, shadow.Evidence)
		}
		if shadow.Evidence.BaselineScoresSHA256 == "" || shadow.Evidence.AblatedScoresSHA256 == "" {
			t.Fatalf("shadow observational drop omitted numeric gate for %s: %+v", intervention, shadow.Evidence)
		}
		enforce, err := Evaluate(evaluateInputFromReport(t, config, report, intervention, ModeEnforce))
		if err != nil {
			t.Fatal(err)
		}
		if enforce.Evidence.Status != StatusUnavailable || enforce.Evidence.Reason != ReasonEnforceProofUnavailable ||
			enforce.Evidence.SemanticFactor != 0 || enforce.Evidence.AppliedFactor != 0 {
			t.Fatalf("adaptive detection crossed enforce boundary for %s: %+v", intervention, enforce.Evidence)
		}
	}
}

func TestGatePublishesPairedMeansAndTrustedCounts(t *testing.T) {
	usage := usageWithCalls(t, InterventionInference, 7)
	input := evaluationInput(InterventionInference, ModeShadow, usage)
	input.Threshold = 0.6
	rebindEvaluationThreshold(&input)
	got, err := Evaluate(input)
	if err != nil {
		t.Fatal(err)
	}
	if got.Evidence.BaselineMean != 0.9 || got.Evidence.AblatedMean != 0.4 || got.Evidence.Delta != 0.5 ||
		got.Evidence.SampleCount != 3 || got.Evidence.AffectedCallCount != 7 || got.Evidence.Threshold != 0.6 {
		t.Fatalf("evidence=%+v", got.Evidence)
	}
	if got.Evidence.ArtifactSHA256 != testArtifactSHA ||
		got.Evidence.AblationProfileSHA256 != input.AblationProfileSHA256 ||
		got.Evidence.CoordinatorSHA256 != input.Coordinator.CoordinatorSHA256 ||
		got.Evidence.SelectedCasesSHA256 != input.Coordinator.SelectedCasesSHA256 ||
		got.Evidence.CaseSetSHA256 != input.Coordinator.SelectedCaseSetSHA256 ||
		got.Evidence.BenchVersion != BenchVersionV9 || got.Evidence.DatasetSHA256 != testDatasetSHA ||
		got.Evidence.ThresholdManifestSHA256 != testManifestSHA ||
		got.Evidence.SemanticFactor != 0 || got.Evidence.AppliedFactor != 1 {
		t.Fatalf("evidence omitted frozen bindings or factors: %+v", got.Evidence)
	}
	if !canonicalSHA256(got.SHA256) || !canonicalSHA256(got.Evidence.CaseSetSHA256) ||
		!canonicalSHA256(got.Evidence.BaselineScoresSHA256) || !canonicalSHA256(got.Evidence.AblatedScoresSHA256) {
		t.Fatalf("invalid evidence digests: %+v", got)
	}
	if !bytes.Equal(got.CanonicalJSON, mustJSON(t, got.Evidence)) {
		t.Fatalf("canonical evidence mismatch:\n%s\n%s", got.CanonicalJSON, mustJSON(t, got.Evidence))
	}
	if bytes.Contains(got.CanonicalJSON, []byte("case-a")) || bytes.Contains(got.CanonicalJSON, []byte("case-b")) {
		t.Fatalf("private case IDs leaked into aggregate evidence: %s", got.CanonicalJSON)
	}
	if got.Evidence.SyntheticUsage.UpstreamRequests != 0 || got.Evidence.SyntheticUsage.UpstreamInputTokens != 0 ||
		got.Evidence.SyntheticUsage.UpstreamOutputTokens != 0 || got.Evidence.SyntheticUsage.UpstreamProviderCostMicroUSD != 0 {
		t.Fatalf("synthetic cost is not zero: %+v", got.Evidence.SyntheticUsage)
	}
	if !bytes.Contains(got.CanonicalJSON, []byte(`"telemetry_namespace":"dittobench.v9.ablation.inference"`)) ||
		bytes.Contains(got.CanonicalJSON, []byte("upstream_provider_cost_usd")) {
		t.Fatalf("synthetic telemetry schema is not namespaced/integer-safe: %s", got.CanonicalJSON)
	}
}

func TestProbeOnlyPositiveDeltasNeverPublishACompletedGate(t *testing.T) {
	t.Parallel()
	for _, intervention := range []Intervention{InterventionInference, InterventionEmbedding} {
		for _, threshold := range []float64{0, 0.000001, 0.2, 0.5} {
			t.Run(string(intervention)+"/threshold", func(t *testing.T) {
				input := evaluationInput(intervention, ModeShadow, usageWithCalls(t, intervention, 6))
				input.Threshold = threshold
				rebindEvaluationThreshold(&input)
				got, err := Evaluate(input)
				if err != nil {
					t.Fatal(err)
				}
				if got.Evidence.Status != StatusFailed || got.Evidence.Reason != ReasonObservationalDropNotCausal {
					t.Fatalf("probe-only positive delta was not a completed non-causal observation: %+v", got.Evidence)
				}
				if got.Evidence.Status == StatusPassed || got.Evidence.Reason == ReasonThresholdMet {
					t.Fatalf("probe-only positive delta qualified as a pass: %+v", got.Evidence)
				}
				if got.Evidence.BaselineScoresSHA256 == "" || got.Evidence.AblatedScoresSHA256 == "" {
					t.Fatalf("probe-only observational drop omitted numeric evidence: %+v", got.Evidence)
				}
				if got.Evidence.SampleCount != len(input.Baseline) || got.Evidence.AffectedCallCount != 6 ||
					got.Evidence.CoordinatorSHA256 != input.Coordinator.CoordinatorSHA256 {
					t.Fatalf("ambiguous result lost private audit commitment: %+v", got.Evidence)
				}
			})
		}
	}
}

func FuzzPositiveShadowDeltaIsNeverPassingEvidence(f *testing.F) {
	f.Add(uint16(9000), uint16(1000), uint16(2000))
	f.Add(uint16(5000), uint16(5000), uint16(0))
	f.Add(uint16(65535), uint16(0), uint16(65535))
	f.Fuzz(func(t *testing.T, baselineRaw, ablatedRaw, thresholdRaw uint16) {
		baseline := float64(baselineRaw) / float64(^uint16(0))
		ablated := float64(ablatedRaw) / float64(^uint16(0))
		threshold := float64(thresholdRaw) / float64(^uint16(0))
		if baseline-ablated < threshold {
			t.Skip()
		}
		input := evaluationInput(InterventionInference, ModeShadow, usageWithCalls(t, InterventionInference, 2))
		input.Baseline = []CaseScore{{CaseID: "fuzz", Score: baseline}}
		input.Ablated = []CaseScore{{CaseID: "fuzz", Score: ablated}}
		rebindEvaluationPopulation(&input)
		input.Threshold = threshold
		rebindEvaluationThreshold(&input)
		got, err := Evaluate(input)
		if err != nil {
			t.Fatal(err)
		}
		if got.Evidence.Status == StatusPassed || got.Evidence.Reason == ReasonThresholdMet {
			t.Fatalf("positive synthetic delta escaped fail-closed classification: %+v", got.Evidence)
		}
		if got.Evidence.Status != StatusFailed || got.Evidence.Reason != ReasonObservationalDropNotCausal {
			t.Fatalf("positive synthetic delta was not a completed non-causal observation: %+v", got.Evidence)
		}
	})
}

func resignCoordinatorForTest(input *EvaluateInput) {
	input.Coordinator.CoordinatorSHA256, _ = coordinatorDigest(input.Coordinator)
}

func TestEvaluateUsesGlobalProfileWithRunSpecificArtifactEvidence(t *testing.T) {
	t.Parallel()
	const secondArtifactSHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	firstInput := evaluationInput(InterventionInference, ModeEnforce, usageWithCalls(t, InterventionInference, 6))
	secondInput := firstInput
	secondInput.ArtifactSHA256 = secondArtifactSHA
	secondInput.Coordinator.ArtifactSHA256 = secondArtifactSHA
	resignCoordinatorForTest(&secondInput)

	first, err := Evaluate(firstInput)
	if err != nil {
		t.Fatal(err)
	}
	second, err := Evaluate(secondInput)
	if err != nil {
		t.Fatal(err)
	}
	if first.Evidence.AblationProfileSHA256 != second.Evidence.AblationProfileSHA256 {
		t.Fatal("per-run artifact moved the global profile checksum")
	}
	if first.Evidence.ArtifactSHA256 != testArtifactSHA || second.Evidence.ArtifactSHA256 != secondArtifactSHA {
		t.Fatalf("evidence lost the per-run artifact: %s/%s", first.Evidence.ArtifactSHA256, second.Evidence.ArtifactSHA256)
	}
	if first.Evidence.CoordinatorSHA256 == second.Evidence.CoordinatorSHA256 || first.SHA256 == second.SHA256 {
		t.Fatal("runtime artifact did not move the run-specific coordinator and evidence checksums")
	}
}

func TestEvaluateRejectsHostileProfileCoordinatorAndTelemetryTamper(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name   string
		mutate func(*EvaluateInput)
	}{
		{"profile checksum", func(value *EvaluateInput) { value.AblationProfileSHA256 = strings.Repeat("9", 64) }},
		{"invalid runtime artifact", func(value *EvaluateInput) { value.ArtifactSHA256 = "not-a-digest" }},
		{"runtime artifact mismatch", func(value *EvaluateInput) { value.ArtifactSHA256 = strings.Repeat("6", 64) }},
		{"coordinator artifact", func(value *EvaluateInput) {
			value.Coordinator.ArtifactSHA256 = strings.Repeat("6", 64)
			resignCoordinatorForTest(value)
		}},
		{"coordinator dataset", func(value *EvaluateInput) {
			value.Coordinator.DatasetSHA256 = strings.Repeat("6", 64)
			resignCoordinatorForTest(value)
		}},
		{"coordinator threshold manifest", func(value *EvaluateInput) {
			value.Coordinator.ThresholdManifestSHA256 = strings.Repeat("6", 64)
			resignCoordinatorForTest(value)
		}},
		{"coordinator bench", func(value *EvaluateInput) {
			value.Coordinator.BenchVersion = 8
			resignCoordinatorForTest(value)
		}},
		{"coordinator policy", func(value *EvaluateInput) {
			value.Coordinator.CoordinatorPolicy.MaxAttempts++
			resignCoordinatorForTest(value)
		}},
		{"selected count", func(value *EvaluateInput) {
			value.Coordinator.SelectedCaseCount++
			resignCoordinatorForTest(value)
		}},
		{"ordered selection digest", func(value *EvaluateInput) {
			value.Coordinator.SelectedCasesSHA256 = strings.Repeat("6", 64)
			resignCoordinatorForTest(value)
		}},
		{"selected case-set digest", func(value *EvaluateInput) {
			value.Coordinator.SelectedCaseSetSHA256 = strings.Repeat("6", 64)
			resignCoordinatorForTest(value)
		}},
		{"coordinator digest", func(value *EvaluateInput) { value.Coordinator.CoordinatorSHA256 = strings.Repeat("6", 64) }},
		{"ordinary synthetic telemetry", func(value *EvaluateInput) {
			value.Coordinator.Ordinary.SyntheticUsage = value.Usage
			resignCoordinatorForTest(value)
		}},
		{"target telemetry cost", func(value *EvaluateInput) {
			value.Usage.UpstreamProviderCostMicroUSD = 1
			value.Coordinator.InferenceIntervention.SyntheticUsage = value.Usage
			resignCoordinatorForTest(value)
		}},
		{"target telemetry input tokens", func(value *EvaluateInput) {
			value.Usage.UpstreamInputTokens = 1
			value.Coordinator.InferenceIntervention.SyntheticUsage = value.Usage
			resignCoordinatorForTest(value)
		}},
		{"target telemetry output tokens", func(value *EvaluateInput) {
			value.Usage.UpstreamOutputTokens = 1
			value.Coordinator.InferenceIntervention.SyntheticUsage = value.Usage
			resignCoordinatorForTest(value)
		}},
		{"target telemetry namespace", func(value *EvaluateInput) {
			value.Usage.TelemetryNamespace = "ordinary"
			value.Coordinator.InferenceIntervention.SyntheticUsage = value.Usage
			resignCoordinatorForTest(value)
		}},
		{"other lane telemetry contamination", func(value *EvaluateInput) {
			value.Coordinator.EmbeddingIntervention.SyntheticUsage.UpstreamOutputTokens = 1
			resignCoordinatorForTest(value)
		}},
		{"usage coordinator inequality", func(value *EvaluateInput) { value.Usage.ChatInputBytes++ }},
	}
	for _, testCase := range tests {
		t.Run(testCase.name, func(t *testing.T) {
			input := evaluationInput(InterventionInference, ModeShadow, usageWithCalls(t, InterventionInference, 6))
			testCase.mutate(&input)
			if _, err := Evaluate(input); err == nil {
				t.Fatal("hostile binding tamper accepted")
			}
		})
	}
}

func mustJSON(t *testing.T, value any) []byte {
	t.Helper()
	body, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func TestGateDigestRejectsSelectionReorderingAndIsTamperSensitive(t *testing.T) {
	input := evaluationInput(InterventionInference, ModeShadow, usageWithCalls(t, InterventionInference, 6))
	first, err := Evaluate(input)
	if err != nil {
		t.Fatal(err)
	}
	input.Baseline[0], input.Baseline[2] = input.Baseline[2], input.Baseline[0]
	input.Ablated[0], input.Ablated[1] = input.Ablated[1], input.Ablated[0]
	if _, err := Evaluate(input); err == nil {
		t.Fatal("pair reordering outside the frozen coordinator selection was accepted")
	}
	input = evaluationInput(InterventionInference, ModeShadow, usageWithCalls(t, InterventionInference, 6))
	tamperedScores := []func(*EvaluateInput){
		func(value *EvaluateInput) {
			value.Baseline[0].Score -= 0.01
			value.Coordinator.Ordinary.Scores = value.Baseline
		},
		func(value *EvaluateInput) {
			value.Ablated[0].Score += 0.01
			value.Coordinator.InferenceIntervention.Scores = value.Ablated
		},
	}
	for index, mutate := range tamperedScores {
		changed := input
		changed.Baseline = append([]CaseScore(nil), input.Baseline...)
		changed.Ablated = append([]CaseScore(nil), input.Ablated...)
		mutate(&changed)
		if _, err := Evaluate(changed); err == nil {
			t.Fatalf("uncommitted score mutation %d was accepted", index)
		}
	}
	digestChangingInputs := []func(*EvaluateInput){
		func(value *EvaluateInput) {
			value.Threshold += 0.01
			rebindEvaluationThreshold(value)
		},
		func(value *EvaluateInput) {
			value.Usage.ChatInputBytes++
			value.Coordinator.InferenceIntervention.SyntheticUsage = value.Usage
			value.Coordinator.CoordinatorSHA256, _ = coordinatorDigest(value.Coordinator)
		},
	}
	for index, mutate := range digestChangingInputs {
		changed := input
		changed.Baseline = append([]CaseScore(nil), input.Baseline...)
		changed.Ablated = append([]CaseScore(nil), input.Ablated...)
		mutate(&changed)
		got, err := Evaluate(changed)
		if err != nil {
			t.Fatalf("mutation %d invalid rather than digest-changing: %v", index, err)
		}
		if got.SHA256 == first.SHA256 {
			t.Errorf("mutation %d did not change evidence digest", index)
		}
	}
}

func TestOffModeProducesNeutralNotRunEvidence(t *testing.T) {
	input := evaluationInput(InterventionInference, ModeOff, usageWithCalls(t, InterventionInference, 0))
	input.Coordinator = CoordinationReport{}
	input.Usage = Usage{}
	input.Baseline = nil
	input.Ablated = nil
	got, err := Evaluate(input)
	if err != nil {
		t.Fatal(err)
	}
	if got.Evidence.Status != StatusNotRun || got.Evidence.Reason != ReasonDisabled ||
		got.Evidence.SemanticFactor != 1 || got.Evidence.AppliedFactor != 1 ||
		got.Evidence.SampleCount != 0 || got.Evidence.DatasetSHA256 != testDatasetSHA ||
		got.Evidence.ArtifactSHA256 != testArtifactSHA || got.Evidence.CoordinatorSHA256 != "" ||
		got.Evidence.SyntheticUsage.affectedCalls() != 0 {
		t.Fatalf("off evidence=%+v", got.Evidence)
	}
	active := usageWithCalls(t, InterventionInference, 1)
	activeInput := evaluationInput(InterventionInference, ModeOff, active)
	if _, err := Evaluate(activeInput); err == nil {
		t.Fatal("off mode accepted synthetic activity")
	}
}

func TestUsageMarshalPreservesRequiredZeroLaneCounters(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name             string
		usage            Usage
		wantUsageKeys    []string
		wantBudgetKeys   []string
		wantZeroCounters []string
		forbiddenKeys    []string
	}{
		{
			name: "inference",
			usage: Usage{
				Synthetic:          true,
				TelemetryNamespace: telemetryNamespace(InterventionInference),
				Intervention:       InterventionInference,
				Budget: Budget{
					MaxChatRequests:   4,
					MaxChatInputBytes: 256,
				},
			},
			wantUsageKeys: []string{
				"synthetic", "telemetry_namespace", "intervention", "budget",
				"chat_attempts", "chat_applied", "chat_input_bytes",
				"upstream_requests", "upstream_input_tokens", "upstream_output_tokens",
				"upstream_provider_cost_microusd",
			},
			wantBudgetKeys:   []string{"max_chat_requests", "max_chat_input_bytes"},
			wantZeroCounters: []string{"chat_attempts", "chat_applied", "chat_input_bytes"},
			forbiddenKeys: []string{
				"embedding_attempts", "embedding_applied", "embedding_inputs", "embedding_input_bytes",
			},
		},
		{
			name: "embedding",
			usage: Usage{
				Synthetic:          true,
				TelemetryNamespace: telemetryNamespace(InterventionEmbedding),
				Intervention:       InterventionEmbedding,
				Budget: Budget{
					MaxEmbeddingRequests:   4,
					MaxEmbeddingInputs:     4,
					MaxEmbeddingInputBytes: 256,
				},
			},
			wantUsageKeys: []string{
				"synthetic", "telemetry_namespace", "intervention", "budget",
				"embedding_attempts", "embedding_applied", "embedding_inputs", "embedding_input_bytes",
				"upstream_requests", "upstream_input_tokens", "upstream_output_tokens",
				"upstream_provider_cost_microusd",
			},
			wantBudgetKeys: []string{
				"max_embedding_requests", "max_embedding_inputs", "max_embedding_input_bytes",
			},
			wantZeroCounters: []string{
				"embedding_attempts", "embedding_applied", "embedding_inputs", "embedding_input_bytes",
			},
			forbiddenKeys: []string{"chat_attempts", "chat_applied", "chat_input_bytes"},
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			raw, err := json.Marshal(test.usage)
			if err != nil {
				t.Fatal(err)
			}
			var wire map[string]json.RawMessage
			if err := json.Unmarshal(raw, &wire); err != nil {
				t.Fatal(err)
			}
			gotKeys := make([]string, 0, len(wire))
			for key := range wire {
				gotKeys = append(gotKeys, key)
			}
			sort.Strings(gotKeys)
			wantKeys := append([]string(nil), test.wantUsageKeys...)
			sort.Strings(wantKeys)
			if !reflect.DeepEqual(gotKeys, wantKeys) {
				t.Fatalf("usage keys = %v, want %v; JSON=%s", gotKeys, wantKeys, raw)
			}
			for _, key := range test.wantZeroCounters {
				if string(wire[key]) != "0" {
					t.Fatalf("%s = %s, want explicit zero; JSON=%s", key, wire[key], raw)
				}
			}
			for _, key := range test.forbiddenKeys {
				if _, exists := wire[key]; exists {
					t.Fatalf("opposite-lane field %q leaked into %s usage: %s", key, test.name, raw)
				}
			}

			var budget map[string]json.RawMessage
			if err := json.Unmarshal(wire["budget"], &budget); err != nil {
				t.Fatal(err)
			}
			gotBudgetKeys := make([]string, 0, len(budget))
			for key := range budget {
				gotBudgetKeys = append(gotBudgetKeys, key)
			}
			sort.Strings(gotBudgetKeys)
			wantBudgetKeys := append([]string(nil), test.wantBudgetKeys...)
			sort.Strings(wantBudgetKeys)
			if !reflect.DeepEqual(gotBudgetKeys, wantBudgetKeys) {
				t.Fatalf("budget keys = %v, want %v; JSON=%s", gotBudgetKeys, wantBudgetKeys, raw)
			}
		})
	}
}

func TestUsageMarshalDoesNotHideOppositeLaneActivity(t *testing.T) {
	t.Parallel()
	usage := Usage{
		Intervention:      InterventionInference,
		EmbeddingAttempts: 1,
	}
	raw, err := json.Marshal(usage)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(raw, []byte(`"embedding_attempts":1`)) {
		t.Fatalf("invalid opposite-lane activity was hidden from the wire: %s", raw)
	}
}

func TestEvaluateRejectsInvalidContractsAndPairs(t *testing.T) {
	valid := evaluationInput(InterventionInference, ModeShadow, usageWithCalls(t, InterventionInference, 6))
	tooLongID := strings.Repeat("x", 257)
	invalid := []struct {
		name   string
		mutate func(*EvaluateInput)
	}{
		{"wrong bench version", func(value *EvaluateInput) { value.BenchVersion = 8 }},
		{"none intervention", func(value *EvaluateInput) { value.Intervention = InterventionNone }},
		{"unknown mode", func(value *EvaluateInput) { value.Mode = "future" }},
		{"missing revision", func(value *EvaluateInput) { value.ProfileRevision = "" }},
		{"space revision", func(value *EvaluateInput) { value.ProfileRevision = " launch" }},
		{"bad manifest digest", func(value *EvaluateInput) { value.ThresholdManifestSHA256 = "AA" }},
		{"bad dataset digest", func(value *EvaluateInput) { value.DatasetSHA256 = strings.Repeat("A", 64) }},
		{"negative threshold", func(value *EvaluateInput) { value.Threshold = -0.1 }},
		{"threshold above one", func(value *EvaluateInput) { value.Threshold = 1.1 }},
		{"nan threshold", func(value *EvaluateInput) { value.Threshold = math.NaN() }},
		{"inf threshold", func(value *EvaluateInput) { value.Threshold = math.Inf(1) }},
		{"empty baseline", func(value *EvaluateInput) { value.Baseline = nil }},
		{"empty ablated", func(value *EvaluateInput) { value.Ablated = nil }},
		{"size mismatch", func(value *EvaluateInput) { value.Ablated = value.Ablated[:1] }},
		{"identity mismatch", func(value *EvaluateInput) { value.Ablated[0].CaseID = "other" }},
		{"duplicate baseline", func(value *EvaluateInput) { value.Baseline[1].CaseID = value.Baseline[0].CaseID }},
		{"duplicate ablated", func(value *EvaluateInput) { value.Ablated[1].CaseID = value.Ablated[0].CaseID }},
		{"empty id", func(value *EvaluateInput) { value.Baseline[0].CaseID = "" }},
		{"space id", func(value *EvaluateInput) { value.Baseline[0].CaseID = " case" }},
		{"long id", func(value *EvaluateInput) { value.Baseline[0].CaseID = tooLongID }},
		{"nan baseline", func(value *EvaluateInput) { value.Baseline[0].Score = math.NaN() }},
		{"inf ablated", func(value *EvaluateInput) { value.Ablated[0].Score = math.Inf(1) }},
		{"negative score", func(value *EvaluateInput) { value.Baseline[0].Score = -0.1 }},
		{"score above one", func(value *EvaluateInput) { value.Ablated[0].Score = 1.1 }},
	}
	for _, testCase := range invalid {
		t.Run(testCase.name, func(t *testing.T) {
			input := valid
			input.Baseline = append([]CaseScore(nil), valid.Baseline...)
			input.Ablated = append([]CaseScore(nil), valid.Ablated...)
			testCase.mutate(&input)
			if _, err := Evaluate(input); err == nil {
				t.Fatal("invalid input accepted")
			}
		})
	}
}

func TestEvaluateRejectsForgedSyntheticUsage(t *testing.T) {
	valid := evaluationInput(InterventionInference, ModeShadow, usageWithCalls(t, InterventionInference, 6))
	invalid := []struct {
		name   string
		mutate func(*Usage)
	}{
		{"not synthetic", func(usage *Usage) { usage.Synthetic = false }},
		{"wrong intervention", func(usage *Usage) { usage.Intervention = InterventionEmbedding }},
		{"upstream requests", func(usage *Usage) { usage.UpstreamRequests = 1 }},
		{"upstream cost", func(usage *Usage) { usage.UpstreamProviderCostMicroUSD = 1 }},
		{"upstream input tokens", func(usage *Usage) { usage.UpstreamInputTokens = 1 }},
		{"upstream output tokens", func(usage *Usage) { usage.UpstreamOutputTokens = 1 }},
		{"wrong telemetry namespace", func(usage *Usage) { usage.TelemetryNamespace = "ordinary" }},
		{"applied exceeds attempts", func(usage *Usage) { usage.ChatApplied = usage.ChatAttempts + 1 }},
		{"rejected without exhaustion", func(usage *Usage) { usage.RejectedRequests = 1; usage.BudgetExhausted = false }},
		{"exhaustion without rejection", func(usage *Usage) { usage.BudgetExhausted = true }},
		{"attempt count mismatch", func(usage *Usage) { usage.ChatAttempts++ }},
		{"cross-lane attempts", func(usage *Usage) { usage.EmbeddingAttempts = 1 }},
		{"applied exceeds budget", func(usage *Usage) {
			usage.ChatApplied = usage.Budget.MaxChatRequests + 1
			usage.ChatAttempts = usage.ChatApplied
		}},
		{"bytes exceed budget", func(usage *Usage) { usage.ChatInputBytes = usage.Budget.MaxChatInputBytes + 1 }},
	}
	for _, testCase := range invalid {
		t.Run(testCase.name, func(t *testing.T) {
			input := valid
			testCase.mutate(&input.Usage)
			if _, err := Evaluate(input); err == nil {
				t.Fatal("forged usage accepted")
			}
		})
	}
}
