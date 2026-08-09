package longmemeval

import (
	"bytes"
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestZeroBudgetSnapshotRequiresExactTrustedIdentity(t *testing.T) {
	profile := loadTestProfile(t)
	for _, policy := range profile.Providers {
		value := newRecordingMeter(profile).values[policy.Lane]
		if err := validateBudgetSnapshot(policy, value); err != nil {
			t.Fatalf("exact zero snapshot rejected: %v", err)
		}
		for name, mutate := range map[string]func(*ProviderEvidence){
			"request":           func(v *ProviderEvidence) { v.Requests = 1 },
			"success":           func(v *ProviderEvidence) { v.Successes = 1 },
			"receipt count":     func(v *ProviderEvidence) { v.ReceiptedRequests = 1 },
			"prompt tokens":     func(v *ProviderEvidence) { v.PromptTokens = 1 },
			"completion tokens": func(v *ProviderEvidence) { v.CompletionTokens = 1 },
			"total tokens":      func(v *ProviderEvidence) { v.TotalTokens = 1 },
			"cost":              func(v *ProviderEvidence) { v.CostUSDmicros = 1 },
			"receipt digest":    func(v *ProviderEvidence) { v.ReceiptSetSHA256 = receiptDigestA },
		} {
			t.Run(policy.Lane+"/"+name, func(t *testing.T) {
				changed := value
				mutate(&changed)
				if err := validateBudgetSnapshot(policy, changed); err == nil {
					t.Fatal("accounting without a valid request accepted")
				}
			})
		}
	}
}

func TestReadBudgetSnapshotRequiresExactLaneCoverage(t *testing.T) {
	profile := loadTestProfile(t)
	valid, err := newRecordingMeter(profile).Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	tests := map[string][]ProviderEvidence{
		"missing":   valid[:1],
		"duplicate": {valid[0], valid[0]},
		"unexpected": {valid[0], func() ProviderEvidence {
			value := valid[1]
			value.Lane = "embedding"
			return value
		}()},
	}
	for name, values := range tests {
		t.Run(name, func(t *testing.T) {
			meter := staticMeter{values: values}
			if _, err := readBudgetSnapshot(context.Background(), profile, meter, true); err == nil {
				t.Fatal("invalid lane coverage accepted")
			}
		})
	}
}

func TestReadBudgetSnapshotRejectsZeroCounterDriftAndIdentityDrift(t *testing.T) {
	profile := loadTestProfile(t)
	valid, err := newRecordingMeter(profile).Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(*ProviderEvidence){
		"provider":         func(v *ProviderEvidence) { v.Provider += "-other" },
		"model":            func(v *ProviderEvidence) { v.Model += "-other" },
		"profile":          func(v *ProviderEvidence) { v.ProfileRevision += "-other" },
		"fallback":         func(v *ProviderEvidence) { v.FallbackUsed = true },
		"cost source":      func(v *ProviderEvidence) { v.CostSource = "estimate" },
		"currency":         func(v *ProviderEvidence) { v.Currency = "EUR" },
		"preexisting cost": func(v *ProviderEvidence) { v.CostUSDmicros = 1 },
	} {
		t.Run(name, func(t *testing.T) {
			changed := append([]ProviderEvidence(nil), valid...)
			mutate(&changed[0])
			if _, err := readBudgetSnapshot(context.Background(), profile, staticMeter{values: changed}, true); err == nil {
				t.Fatal("invalid initial snapshot accepted")
			}
		})
	}
}

func TestValidateMonotonicSnapshotRejectsEveryCounterRegression(t *testing.T) {
	profile := loadTestProfile(t)
	meter := newRecordingMeter(profile)
	for _, lane := range []string{ReaderLane, JudgeLane} {
		meter.add(lane, 10, 5, 3, true)
		meter.add(lane, 10, 5, 3, true)
	}
	values, err := meter.Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	before := make(map[string]ProviderEvidence)
	for _, value := range values {
		before[value.Lane] = value
	}
	for name, mutate := range map[string]func(*ProviderEvidence){
		"requests":   func(v *ProviderEvidence) { v.Requests-- },
		"successes":  func(v *ProviderEvidence) { v.Successes-- },
		"receipts":   func(v *ProviderEvidence) { v.ReceiptedRequests-- },
		"prompt":     func(v *ProviderEvidence) { v.PromptTokens-- },
		"completion": func(v *ProviderEvidence) { v.CompletionTokens-- },
		"total":      func(v *ProviderEvidence) { v.TotalTokens-- },
		"cost":       func(v *ProviderEvidence) { v.CostUSDmicros-- },
	} {
		t.Run(name, func(t *testing.T) {
			after := cloneSnapshotMap(before)
			mutateSnapshot := after[ReaderLane]
			mutate(&mutateSnapshot)
			after[ReaderLane] = mutateSnapshot
			if err := validateMonotonicSnapshot(before, after); err == nil {
				t.Fatal("regressing provider counter accepted")
			}
		})
	}
}

func TestValidateMonotonicSnapshotRejectsCoverageAndReceiptRewrites(t *testing.T) {
	profile := loadTestProfile(t)
	meter := newRecordingMeter(profile)
	meter.add(ReaderLane, 10, 2, 5, true)
	values, err := meter.Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	before := make(map[string]ProviderEvidence)
	for _, value := range values {
		before[value.Lane] = value
	}

	missing := cloneSnapshotMap(before)
	delete(missing, JudgeLane)
	if err := validateMonotonicSnapshot(before, missing); err == nil {
		t.Fatal("disappearing lane accepted")
	}
	extra := cloneSnapshotMap(before)
	extra["embedding"] = ProviderEvidence{Lane: "embedding"}
	if err := validateMonotonicSnapshot(before, extra); err == nil {
		t.Fatal("changed lane coverage accepted")
	}
	rewritten := cloneSnapshotMap(before)
	value := rewritten[ReaderLane]
	value.ReceiptSetSHA256 = strings.Repeat("e", 64)
	rewritten[ReaderLane] = value
	if err := validateMonotonicSnapshot(before, rewritten); err == nil {
		t.Fatal("receipt-set rewrite without request accepted")
	}
	stale := cloneSnapshotMap(before)
	value = stale[ReaderLane]
	value.Requests++
	value.Successes++
	value.ReceiptedRequests++
	stale[ReaderLane] = value
	if err := validateMonotonicSnapshot(before, stale); err == nil {
		t.Fatal("new request without new receipt-set commitment accepted")
	}

	advanced := cloneSnapshotMap(before)
	value = advanced[ReaderLane]
	value.Requests++
	value.Successes++
	value.ReceiptedRequests++
	value.ReceiptSetSHA256 = strings.Repeat("e", 64)
	advanced[ReaderLane] = value
	if err := validateMonotonicSnapshot(before, advanced); err != nil {
		t.Fatalf("monotonic receipt-set advance rejected: %v", err)
	}
}

func TestRequireLaneHeadroomFailsClosed(t *testing.T) {
	profile := loadTestProfile(t)
	values, err := newRecordingMeter(profile).Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	current := make(map[string]ProviderEvidence)
	for _, value := range values {
		current[value.Lane] = value
	}
	if err := requireLaneHeadroom(profile, current, ReaderLane); err != nil {
		t.Fatal(err)
	}
	reader := current[ReaderLane]
	reader.Requests = providerPolicy(&profile, ReaderLane).MaxRequests
	current[ReaderLane] = reader
	if err := requireLaneHeadroom(profile, current, ReaderLane); err == nil {
		t.Fatal("exhausted lane retained request headroom")
	}
	if err := requireLaneHeadroom(profile, current, "embedding"); err == nil {
		t.Fatal("missing lane retained request headroom")
	}
}

func TestSnapshotSliceIsCanonicalAndIndependent(t *testing.T) {
	profile := loadTestProfile(t)
	values, err := newRecordingMeter(profile).Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	snapshot := map[string]ProviderEvidence{values[0].Lane: values[0], values[1].Lane: values[1]}
	first := snapshotSlice(snapshot)
	if len(first) != 2 || first[0].Lane != JudgeLane || first[1].Lane != ReaderLane {
		t.Fatalf("canonical snapshot=%#v", first)
	}
	first[0].Provider = "mutated"
	if snapshot[JudgeLane].Provider == "mutated" {
		t.Fatal("caller mutated source snapshot map")
	}
}

func TestCloneDatasetCaseDeepCopiesAllSlices(t *testing.T) {
	original := runtimeDatasetCases()[0]
	cloned := cloneDatasetCase(original)
	cloned.HaystackSessionIDs[0] = "changed"
	cloned.HaystackDates[0] = "changed"
	cloned.AnswerSessionIDs[0] = "changed"
	cloned.HaystackSessions[0][0].Content = "changed"
	if reflect.DeepEqual(original, cloned) == true {
		t.Fatal("mutated clone remained equal")
	}
	if original.HaystackSessionIDs[0] == "changed" || original.HaystackDates[0] == "changed" ||
		original.AnswerSessionIDs[0] == "changed" || original.HaystackSessions[0][0].Content == "changed" {
		t.Fatal("clone mutation changed trusted dataset source")
	}
}

func TestExecutorRejectsProviderAccountingRegressionBetweenOperations(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	zeroValues, err := newRecordingMeter(profile).Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	zero := append([]ProviderEvidence(nil), zeroValues...)
	afterRun := append([]ProviderEvidence(nil), zeroValues...)
	for index := range afterRun {
		if afterRun[index].Lane == ReaderLane {
			afterRun[index].Requests = 1
			afterRun[index].Successes = 1
			afterRun[index].ReceiptedRequests = 1
			afterRun[index].PromptTokens = 10
			afterRun[index].CompletionTokens = 2
			afterRun[index].TotalTokens = 12
			afterRun[index].CostUSDmicros = 5
			afterRun[index].ReceiptSetSHA256 = receiptDigestA
		}
	}
	afterJudgeRegression := append([]ProviderEvidence(nil), zeroValues...)
	for index := range afterJudgeRegression {
		if afterJudgeRegression[index].Lane == JudgeLane {
			afterJudgeRegression[index].Requests = 1
			afterJudgeRegression[index].Successes = 1
			afterJudgeRegression[index].ReceiptedRequests = 1
			afterJudgeRegression[index].PromptTokens = 3
			afterJudgeRegression[index].CompletionTokens = 2
			afterJudgeRegression[index].TotalTokens = 5
			afterJudgeRegression[index].CostUSDmicros = 7
			afterJudgeRegression[index].ReceiptSetSHA256 = receiptDigestB
		}
	}
	meter := &scriptedMeter{snapshots: [][]ProviderEvidence{zero, zero, afterRun, afterJudgeRegression}}
	harness := newStarterHarness(nil)
	judge := &exactJudge{}
	executor := Executor{Harness: harness, Judge: judge, Meter: meter, Limits: ExecutionLimits{MaxElapsed: time.Second, SeedBatchPairs: 64}}
	_, err = executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
	requireErrorContains(t, err, "accounting regressed")
}

func TestExecutorRejectsMeterFailureAfterProviderOperation(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	zero, err := newRecordingMeter(profile).Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	meter := &scriptedMeter{
		snapshots: [][]ProviderEvidence{zero, zero},
		errors:    map[int]error{2: errors.New("trusted meter unavailable")},
	}
	harness := newStarterHarness(nil)
	executor := Executor{Harness: harness, Judge: &exactJudge{}, Meter: meter, Limits: ExecutionLimits{MaxElapsed: time.Second, SeedBatchPairs: 64}}
	_, err = executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
	requireErrorContains(t, err, "trusted meter unavailable")
}

type staticMeter struct{ values []ProviderEvidence }

func (m staticMeter) Snapshot(context.Context) ([]ProviderEvidence, error) {
	return append([]ProviderEvidence(nil), m.values...), nil
}

type scriptedMeter struct {
	index     int
	snapshots [][]ProviderEvidence
	errors    map[int]error
}

func (m *scriptedMeter) Snapshot(context.Context) ([]ProviderEvidence, error) {
	index := m.index
	m.index++
	if err := m.errors[index]; err != nil {
		return nil, err
	}
	if index >= len(m.snapshots) {
		return nil, errors.New("unexpected snapshot")
	}
	return append([]ProviderEvidence(nil), m.snapshots[index]...), nil
}

func cloneSnapshotMap(source map[string]ProviderEvidence) map[string]ProviderEvidence {
	result := make(map[string]ProviderEvidence, len(source))
	for lane, value := range source {
		result[lane] = value
	}
	return result
}
