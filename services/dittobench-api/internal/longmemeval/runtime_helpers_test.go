package longmemeval

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

var fixtureProjectionKey = []byte("0123456789abcdef0123456789abcdef")

func runtimeDatasetCases() []DatasetCase {
	types := []struct {
		prefix       string
		questionType string
		abstention   bool
	}{
		{"extract", "single-session-user", false},
		{"multi", "multi-session", false},
		{"temporal", "temporal-reasoning", false},
		{"update", "knowledge-update", false},
		{"preference", "single-session-preference", false},
		{"abstain", "multi-session", true},
	}
	result := make([]DatasetCase, 0, 12)
	for typeIndex, item := range types {
		for index := 0; index < 2; index++ {
			questionID := fmt.Sprintf("private-%s-%02d", item.prefix, index)
			if item.abstention {
				questionID += "_abs"
			}
			answer := fmt.Sprintf("passphrase-%02d-%02d", typeIndex, index)
			result = append(result, DatasetCase{
				QuestionID:         questionID,
				QuestionType:       item.questionType,
				Question:           "What passphrase did I ask you to remember?",
				Answer:             answer,
				QuestionDate:       "2026/01/02 (Fri) 12:00",
				HaystackSessionIDs: []string{fmt.Sprintf("%s-origin-provenance-%02d", item.prefix, index)},
				HaystackDates:      []string{"2025/01/01 (Wed) 10:00"},
				HaystackSessions: [][]DatasetTurn{{
					{Role: "user", Content: "Remember passphrase " + answer, HasAnswer: true},
					{Role: "assistant", Content: "I stored it.", HasAnswer: true},
				}},
				AnswerSessionIDs: []string{fmt.Sprintf("answer-proof-%02d-%02d", typeIndex, index)},
			})
		}
	}
	return result
}

func runtimeFixture(t *testing.T) (Profile, []byte, LoadedDataset) {
	t.Helper()
	entries := runtimeDatasetCases()
	raw, err := json.Marshal(entries)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(raw)
	profile := loadTestProfile(t)
	profile.Revision = "longmemeval-s-v9-shadow-test-only"
	profile.DatasetRevision = "test-fixture-cleaned-revision"
	profile.DatasetSHA256 = hex.EncodeToString(digest[:])
	profile.CasesPerCapability = 2
	dataset, err := LoadSelectedDataset(context.Background(), bytes.NewReader(raw), profile)
	if err != nil {
		t.Fatal(err)
	}
	return profile, raw, dataset
}

type recordingMeter struct {
	mu          sync.Mutex
	values      map[string]ProviderEvidence
	snapshotErr error
}

func newRecordingMeter(profile Profile) *recordingMeter {
	values := make(map[string]ProviderEvidence, len(profile.Providers))
	for _, policy := range profile.Providers {
		values[policy.Lane] = ProviderEvidence{
			Lane:            policy.Lane,
			CostSource:      AuthoritativeCostV1,
			Currency:        "USD",
			Provider:        policy.Provider,
			ProfileRevision: policy.ProfileRevision,
			Model:           policy.Model,
		}
	}
	return &recordingMeter{values: values}
}

func (m *recordingMeter) Snapshot(context.Context) ([]ProviderEvidence, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.snapshotErr != nil {
		return nil, m.snapshotErr
	}
	result := make([]ProviderEvidence, 0, len(m.values))
	for _, value := range m.values {
		result = append(result, value)
	}
	return result, nil
}

func (m *recordingMeter) add(lane string, prompt, completion, cost uint64, success bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	value := m.values[lane]
	value.Requests++
	value.ReceiptedRequests++
	if success {
		value.Successes++
	}
	value.PromptTokens += prompt
	value.CompletionTokens += completion
	value.TotalTokens = value.PromptTokens + value.CompletionTokens
	value.CostUSDmicros += cost
	digest := sha256.Sum256([]byte(fmt.Sprintf("%s-receipts-%d", lane, value.Requests)))
	value.ReceiptSetSHA256 = hex.EncodeToString(digest[:])
	m.values[lane] = value
}

func (m *recordingMeter) mutate(lane string, mutate func(*ProviderEvidence)) {
	m.mu.Lock()
	defer m.mu.Unlock()
	value := m.values[lane]
	mutate(&value)
	m.values[lane] = value
}

type starterHarness struct {
	mu        sync.Mutex
	meter     *recordingMeter
	stores    map[string]map[string]protocol.MemoryPair
	seedUsers []string
	runUsers  []string
	runs      int
	failSeed  error
	failRun   error
	blockRun  <-chan struct{}
}

func newStarterHarness(meter *recordingMeter) *starterHarness {
	return &starterHarness{meter: meter, stores: make(map[string]map[string]protocol.MemoryPair)}
}

func (h *starterHarness) Seed(_ context.Context, request protocol.SeedRequest) (protocol.SeedResponse, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.failSeed != nil {
		return protocol.SeedResponse{}, h.failSeed
	}
	store := h.stores[request.UserID]
	if store == nil {
		store = make(map[string]protocol.MemoryPair)
		h.stores[request.UserID] = store
	}
	for _, pair := range request.Pairs {
		store[pair.PairID] = pair
	}
	h.seedUsers = append(h.seedUsers, request.UserID)
	return protocol.SeedResponse{Pairs: len(request.Pairs), Subjects: len(request.Subjects), Links: len(request.Links)}, nil
}

func (h *starterHarness) Run(ctx context.Context, request protocol.RunRequest) (protocol.RunResponse, error) {
	if h.blockRun != nil {
		select {
		case <-ctx.Done():
			return protocol.RunResponse{}, ctx.Err()
		case <-h.blockRun:
		}
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.failRun != nil {
		return protocol.RunResponse{}, h.failRun
	}
	h.runs++
	h.runUsers = append(h.runUsers, request.UserID)
	answer := "not found"
	for _, pair := range h.stores[request.UserID] {
		if strings.HasPrefix(pair.Prompt, "Remember passphrase ") {
			answer = strings.TrimPrefix(pair.Prompt, "Remember passphrase ")
		}
	}
	if h.meter != nil {
		h.meter.add(ReaderLane, 10, 2, 5, true)
	}
	return protocol.RunResponse{FinalText: answer, ToolCalls: []protocol.ObservedToolCall{}}, nil
}

type exactJudge struct {
	meter  *recordingMeter
	err    error
	inputs []JudgeInput
}

func (j *exactJudge) Judge(_ context.Context, input JudgeInput) (bool, error) {
	if j.err != nil {
		return false, j.err
	}
	j.inputs = append(j.inputs, input)
	if j.meter != nil {
		j.meter.add(JudgeLane, 3, 2, 7, true)
	}
	return input.Hypothesis == input.Reference.Answer, nil
}

func validExecutor(profile Profile) (Executor, *starterHarness, *exactJudge, *recordingMeter) {
	meter := newRecordingMeter(profile)
	harness := newStarterHarness(meter)
	judge := &exactJudge{meter: meter}
	executor := Executor{
		Harness: harness,
		Judge:   judge,
		Meter:   meter,
		Limits:  ExecutionLimits{MaxElapsed: 5_000_000_000, SeedBatchPairs: 64},
	}
	return executor, harness, judge, meter
}

func wireJSON(t *testing.T, projected []ProjectedCase) []byte {
	t.Helper()
	type wireCase struct {
		Seeds []protocol.SeedRequest `json:"seeds"`
		Run   protocol.RunRequest    `json:"run"`
	}
	values := make([]wireCase, 0, len(projected))
	for _, item := range projected {
		values = append(values, wireCase{Seeds: item.SeedRequests, Run: item.RunRequest})
	}
	raw, err := json.MarshalIndent(values, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return append(raw, '\n')
}

func requireErrorContains(t *testing.T, err error, fragment string) {
	t.Helper()
	if err == nil || !strings.Contains(err.Error(), fragment) {
		t.Fatalf("error=%v, want fragment %q", err, fragment)
	}
}

var errFixture = errors.New("fixture failure")
