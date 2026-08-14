package ablation

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"reflect"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type caseRunnerFunc func(context.Context, RunRequest) (CaseRunResult, error)

func (f caseRunnerFunc) RunCase(ctx context.Context, request RunRequest) (CaseRunResult, error) {
	return f(ctx, request)
}

func coordinatorConfig(sampleSize int) CoordinatorConfig {
	config := CoordinatorConfig{
		SampleSize: sampleSize, MaxAttempts: 3, MaxRequests: sampleSize * 3 * 3,
		RequestTimeout: 500 * time.Millisecond, TotalTimeout: 5 * time.Second,
		ArtifactSHA256: testArtifactSHA,
		SelectionKey:   []byte("selection-key-32-bytes-long-fixed!"),
		ProjectionKey:  []byte("projection-key-32-bytes-long-fixed"),
	}
	refreshCoordinatorProfile(&config)
	return config
}

func refreshCoordinatorProfile(config *CoordinatorConfig) {
	policy, err := config.policy()
	if err != nil {
		panic(err)
	}
	config.FrozenProfile = FrozenProfile{
		ContractVersion: ProfileContractVersion, Revision: "launch-v1", BenchVersion: BenchVersionV9,
		DatasetSHA256:           testDatasetSHA,
		ThresholdManifestSHA256: testManifestSHA, CoordinatorPolicy: policy,
		SelectionKeySHA256: bytesSHA256(config.SelectionKey), ProjectionKeySHA256: bytesSHA256(config.ProjectionKey),
		InferenceBudget:    inferenceBudget(uint64(config.SampleSize*4), uint64(config.SampleSize*1024)),
		EmbeddingBudget:    embeddingBudget(uint64(config.SampleSize*4), uint64(config.SampleSize*8), uint64(config.SampleSize*1024)),
		InferenceThreshold: 0.2, EmbeddingThreshold: 0.2,
	}
	config.ProfileSHA256, err = FrozenProfileSHA256(config.FrozenProfile)
	if err != nil {
		panic(err)
	}
}

func eligiblePopulation(size int) EligiblePopulation {
	cases := make([]EligibleCase, size)
	for index := range cases {
		cases[index] = EligibleCase{
			CaseID: fmt.Sprintf("case-%03d", index),
			UserID: fmt.Sprintf("private-user-%03d", index),
		}
	}
	return EligiblePopulation{BenchVersion: BenchVersionV9, Confirmation: true, Cases: cases}
}

func responders(t *testing.T, sampleSize int) (*Responder, *Responder) {
	t.Helper()
	inference, err := NewResponder(InterventionInference, inferenceBudget(uint64(sampleSize*4), uint64(sampleSize*1024)))
	if err != nil {
		t.Fatal(err)
	}
	embedding, err := NewResponder(InterventionEmbedding, embeddingBudget(uint64(sampleSize*4), uint64(sampleSize*8), uint64(sampleSize*1024)))
	if err != nil {
		t.Fatal(err)
	}
	return inference, embedding
}

func useSynthetic(request RunRequest) error {
	switch request.Lane {
	case LaneOrdinary:
		if request.Responder != nil {
			return errors.New("ordinary lane received a synthetic responder")
		}
	case LaneInference:
		if request.Responder == nil {
			return errors.New("inference lane missing responder")
		}
		if _, err := request.Responder.Chat("openai/gpt-oss-20b", 16); err != nil {
			return err
		}
		_, err := request.Responder.Chat("openai/gpt-oss-20b", 24)
		return err
	case LaneEmbedding:
		if request.Responder == nil {
			return errors.New("embedding lane missing responder")
		}
		if _, err := request.Responder.Embeddings([]string{"bounded text"}); err != nil {
			return err
		}
		_, err := request.Responder.Embeddings([]string{"independent bounded text"})
		return err
	default:
		return errors.New("unknown lane")
	}
	return nil
}

func newCoordinator(t *testing.T, config CoordinatorConfig) *Coordinator {
	t.Helper()
	coordinator, err := NewCoordinator(config)
	if err != nil {
		t.Fatal(err)
	}
	return coordinator
}

func TestCoordinatorConfigAndPopulationAreStrictlyBounded(t *testing.T) {
	t.Parallel()
	valid := coordinatorConfig(2)
	mutations := []struct {
		name   string
		mutate func(*CoordinatorConfig)
	}{
		{"zero sample", func(value *CoordinatorConfig) { value.SampleSize = 0 }},
		{"sample hard limit", func(value *CoordinatorConfig) { value.SampleSize = maximumPairedCases + 1 }},
		{"zero attempts", func(value *CoordinatorConfig) { value.MaxAttempts = 0 }},
		{"attempt hard limit", func(value *CoordinatorConfig) { value.MaxAttempts = maximumAttemptsPerCase + 1 }},
		{"requests below one pass", func(value *CoordinatorConfig) { value.MaxRequests = value.SampleSize*3 - 1 }},
		{"requests above useful", func(value *CoordinatorConfig) { value.MaxRequests = value.SampleSize*3*value.MaxAttempts + 1 }},
		{"requests hard limit", func(value *CoordinatorConfig) {
			value.SampleSize = maximumPairedCases
			value.MaxAttempts = maximumAttemptsPerCase
			value.MaxRequests = maximumCoordinatorRuns + 1
		}},
		{"zero request timeout", func(value *CoordinatorConfig) { value.RequestTimeout = 0 }},
		{"request timeout hard limit", func(value *CoordinatorConfig) { value.RequestTimeout = maximumRequestTimeout + 1 }},
		{"zero total timeout", func(value *CoordinatorConfig) { value.TotalTimeout = 0 }},
		{"total below request", func(value *CoordinatorConfig) { value.TotalTimeout = value.RequestTimeout / 2 }},
		{"total timeout hard limit", func(value *CoordinatorConfig) { value.TotalTimeout = maximumTotalTimeout + 1 }},
		{"short selection key", func(value *CoordinatorConfig) { value.SelectionKey = []byte("short") }},
		{"short projection key", func(value *CoordinatorConfig) { value.ProjectionKey = []byte("short") }},
		{"oversized selection key", func(value *CoordinatorConfig) { value.SelectionKey = make([]byte, maximumProjectionBytes+1) }},
		{"oversized projection key", func(value *CoordinatorConfig) { value.ProjectionKey = make([]byte, maximumProjectionBytes+1) }},
	}
	for _, testCase := range mutations {
		t.Run(testCase.name, func(t *testing.T) {
			config := valid
			testCase.mutate(&config)
			if _, err := NewCoordinator(config); err == nil {
				t.Fatal("invalid coordinator config accepted")
			}
		})
	}

	coordinator := newCoordinator(t, valid)
	populationMutations := []struct {
		name   string
		mutate func(*EligiblePopulation)
	}{
		{"v8", func(value *EligiblePopulation) { value.BenchVersion = 8 }},
		{"not confirmation", func(value *EligiblePopulation) { value.Confirmation = false }},
		{"too small", func(value *EligiblePopulation) { value.Cases = value.Cases[:1] }},
		{"empty case id", func(value *EligiblePopulation) { value.Cases[0].CaseID = "" }},
		{"space case id", func(value *EligiblePopulation) { value.Cases[0].CaseID = " case" }},
		{"empty user id", func(value *EligiblePopulation) { value.Cases[0].UserID = "" }},
		{"space user id", func(value *EligiblePopulation) { value.Cases[0].UserID = "user " }},
		{"duplicate case", func(value *EligiblePopulation) { value.Cases[1].CaseID = value.Cases[0].CaseID }},
	}
	for _, testCase := range populationMutations {
		t.Run(testCase.name, func(t *testing.T) {
			population := eligiblePopulation(3)
			testCase.mutate(&population)
			if _, _, err := coordinator.selectCases(population); err == nil {
				t.Fatal("invalid population accepted")
			}
		})
	}
	tooLarge := EligiblePopulation{
		BenchVersion: 9, Confirmation: true,
		Cases: make([]EligibleCase, maximumEligibleCases+1),
	}
	if _, _, err := coordinator.selectCases(tooLarge); err == nil {
		t.Fatal("eligible population hard limit was not enforced before scanning")
	}
}

func TestCoordinatorRejectsFrozenProfileAndConfigMismatch(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name   string
		mutate func(*CoordinatorConfig)
	}{
		{"stale profile checksum", func(value *CoordinatorConfig) { value.ProfileSHA256 = strings.Repeat("9", 64) }},
		{"invalid runtime artifact", func(value *CoordinatorConfig) { value.ArtifactSHA256 = "not-a-digest" }},
		{"policy mismatch", func(value *CoordinatorConfig) { value.MaxAttempts-- }},
		{"inference budget invalid", func(value *CoordinatorConfig) {
			value.FrozenProfile.InferenceBudget.MaxChatRequests = 0
		}},
	}
	for _, testCase := range tests {
		t.Run(testCase.name, func(t *testing.T) {
			config := coordinatorConfig(2)
			testCase.mutate(&config)
			if _, err := NewCoordinator(config); err == nil {
				t.Fatal("frozen profile/config mismatch accepted")
			}
		})
	}
}

func TestSelectionIsDeterministicFixedAndInputOrderIndependent(t *testing.T) {
	t.Parallel()
	config := coordinatorConfig(4)
	selectionKey := config.SelectionKey
	projectionKey := config.ProjectionKey
	coordinator := newCoordinator(t, config)
	for index := range selectionKey {
		selectionKey[index] = 'x'
	}
	for index := range projectionKey {
		projectionKey[index] = 'y'
	}

	population := eligiblePopulation(12)
	first, firstDigest, err := coordinator.selectCases(population)
	if err != nil {
		t.Fatal(err)
	}
	for left, right := 0, len(population.Cases)-1; left < right; left, right = left+1, right-1 {
		population.Cases[left], population.Cases[right] = population.Cases[right], population.Cases[left]
	}
	second, secondDigest, err := coordinator.selectCases(population)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(first, second) || firstDigest != secondDigest || !canonicalSHA256(firstDigest) {
		t.Fatalf("selection changed with input/key mutation: first=%v second=%v digests=%s/%s", first, second, firstDigest, secondDigest)
	}
}

func TestCoordinatorHasNoUpstreamClientOrEndpointFields(t *testing.T) {
	t.Parallel()
	typeOfCoordinator := reflect.TypeOf(Coordinator{})
	want := map[string]reflect.Type{
		"config":         reflect.TypeOf(CoordinatorConfig{}),
		"artifactSHA256": reflect.TypeOf(""),
		"selectionKey":   reflect.TypeOf([]byte(nil)),
		"projectionKey":  reflect.TypeOf([]byte(nil)),
		"profile":        reflect.TypeOf(FrozenProfile{}),
		"profileSHA256":  reflect.TypeOf(""),
		"policy":         reflect.TypeOf(CoordinatorPolicy{}),
	}
	if typeOfCoordinator.NumField() != len(want) {
		t.Fatalf("Coordinator has %d fields, want %d bounded configuration fields", typeOfCoordinator.NumField(), len(want))
	}
	for index := 0; index < typeOfCoordinator.NumField(); index++ {
		field := typeOfCoordinator.Field(index)
		if want[field.Name] != field.Type {
			t.Fatalf("unexpected coordinator field %s %s", field.Name, field.Type)
		}
	}
}

func TestCoordinatorProfileIsGlobalAndArtifactBindingIsRunSpecific(t *testing.T) {
	t.Parallel()
	const secondArtifactSHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	run := func(artifactSHA256 string) (string, CoordinationReport) {
		t.Helper()
		config := coordinatorConfig(2)
		config.ArtifactSHA256 = artifactSHA256
		profileSHA256 := config.ProfileSHA256
		coordinator := newCoordinator(t, config)
		inference, embedding := responders(t, 2)
		report, err := coordinator.Coordinate(
			context.Background(),
			eligiblePopulation(4),
			caseRunnerFunc(func(_ context.Context, request RunRequest) (CaseRunResult, error) {
				if err := useSynthetic(request); err != nil {
					return CaseRunResult{}, err
				}
				return CaseRunResult{Score: 0.5}, nil
			}),
			inference,
			embedding,
		)
		if err != nil {
			t.Fatal(err)
		}
		return profileSHA256, report
	}

	firstProfileSHA256, first := run(testArtifactSHA)
	secondProfileSHA256, second := run(secondArtifactSHA)
	if firstProfileSHA256 != secondProfileSHA256 || first.AblationProfileSHA256 != second.AblationProfileSHA256 {
		t.Fatalf("per-run artifact moved the global profile checksum: %s/%s reports=%s/%s",
			firstProfileSHA256, secondProfileSHA256, first.AblationProfileSHA256, second.AblationProfileSHA256)
	}
	if first.ArtifactSHA256 != testArtifactSHA || second.ArtifactSHA256 != secondArtifactSHA {
		t.Fatalf("coordinator reports did not retain their runtime artifacts: %s/%s", first.ArtifactSHA256, second.ArtifactSHA256)
	}
	if first.CoordinatorSHA256 == second.CoordinatorSHA256 {
		t.Fatal("runtime artifact did not move the run-specific coordinator checksum")
	}
	if first.SelectedCasesSHA256 != second.SelectedCasesSHA256 || first.SelectedCaseSetSHA256 != second.SelectedCaseSetSHA256 {
		t.Fatal("runtime artifact changed the globally frozen selection")
	}
}

type recordedRequest struct {
	Lane      Lane
	CaseID    string
	Namespace string
}

func TestCoordinatorUsesSameCasesAndIsolatedOpaqueNamespaces(t *testing.T) {
	t.Parallel()
	coordinator := newCoordinator(t, coordinatorConfig(5))
	inference, embedding := responders(t, 5)
	var mu sync.Mutex
	var recorded []recordedRequest
	runner := caseRunnerFunc(func(_ context.Context, request RunRequest) (CaseRunResult, error) {
		if err := useSynthetic(request); err != nil {
			return CaseRunResult{}, err
		}
		mu.Lock()
		recorded = append(recorded, recordedRequest{request.Lane, request.CaseID, request.OpaqueUserNamespace})
		mu.Unlock()
		return CaseRunResult{Score: 0.75}, nil
	})
	report, err := coordinator.Coordinate(context.Background(), eligiblePopulation(20), runner, inference, embedding)
	if err != nil {
		t.Fatal(err)
	}
	if !report.Ordinary.Complete || !report.InferenceIntervention.Complete || !report.EmbeddingIntervention.Complete {
		t.Fatalf("incomplete report: %+v", report)
	}
	recomputedCoordinatorSHA256, err := coordinatorDigest(report)
	if err != nil {
		t.Fatal(err)
	}
	if report.ArtifactSHA256 != testArtifactSHA || report.DatasetSHA256 != testDatasetSHA ||
		report.ThresholdManifestSHA256 != testManifestSHA || !canonicalSHA256(report.AblationProfileSHA256) ||
		!canonicalSHA256(report.SelectedCasesSHA256) || !canonicalSHA256(report.SelectedCaseSetSHA256) ||
		report.CoordinatorSHA256 != recomputedCoordinatorSHA256 {
		t.Fatalf("coordination bindings are incomplete: %+v", report)
	}
	if report.InferenceIntervention.SyntheticUsage.TelemetryNamespace != telemetryNamespace(InterventionInference) ||
		report.EmbeddingIntervention.SyntheticUsage.TelemetryNamespace != telemetryNamespace(InterventionEmbedding) {
		t.Fatalf("synthetic telemetry namespaces are not isolated: %+v", report)
	}
	if report.InferenceIntervention.Observation != ObservationInvariant || report.EmbeddingIntervention.Observation != ObservationInvariant {
		t.Fatalf("equal scores were not explicitly invariant: %+v", report)
	}

	byLane := map[Lane][]recordedRequest{}
	for _, request := range recorded {
		byLane[request.Lane] = append(byLane[request.Lane], request)
		if !strings.HasPrefix(request.Namespace, "usr_") || len(request.Namespace) != 4+sha256.Size*2 || strings.Contains(request.Namespace, "private-user") {
			t.Fatalf("namespace is not opaque: %q", request.Namespace)
		}
	}
	for _, lane := range []Lane{LaneOrdinary, LaneInference, LaneEmbedding} {
		if len(byLane[lane]) != 5 {
			t.Fatalf("lane %s requests=%d", lane, len(byLane[lane]))
		}
	}
	for index := range byLane[LaneOrdinary] {
		ordinary := byLane[LaneOrdinary][index]
		inferenceRequest := byLane[LaneInference][index]
		embeddingRequest := byLane[LaneEmbedding][index]
		if ordinary.CaseID != inferenceRequest.CaseID || ordinary.CaseID != embeddingRequest.CaseID {
			t.Fatalf("case identity mismatch at %d: %v/%v/%v", index, ordinary, inferenceRequest, embeddingRequest)
		}
		if ordinary.Namespace == inferenceRequest.Namespace || ordinary.Namespace == embeddingRequest.Namespace || inferenceRequest.Namespace == embeddingRequest.Namespace {
			t.Fatalf("lane namespaces linked at %d: %v/%v/%v", index, ordinary, inferenceRequest, embeddingRequest)
		}
	}
	publicReport, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(publicReport), "private-user") || strings.Contains(string(publicReport), "case-") {
		t.Fatalf("coordination report leaked private identities: %s", publicReport)
	}
}

func TestDifferentProjectionKeysAreUnlinkableButSelectionStaysFixed(t *testing.T) {
	t.Parallel()
	firstConfig := coordinatorConfig(3)
	secondConfig := coordinatorConfig(3)
	secondConfig.ProjectionKey = []byte("another-projection-key-32-bytes!!")
	refreshCoordinatorProfile(&secondConfig)
	first := newCoordinator(t, firstConfig)
	second := newCoordinator(t, secondConfig)
	population := eligiblePopulation(8)
	firstCases, firstDigest, err := first.selectCases(population)
	if err != nil {
		t.Fatal(err)
	}
	secondCases, secondDigest, err := second.selectCases(population)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(firstCases, secondCases) || firstDigest != secondDigest {
		t.Fatal("projection key changed fixed case selection")
	}
	for _, candidate := range firstCases {
		for _, lane := range []Lane{LaneOrdinary, LaneInference, LaneEmbedding} {
			if first.userNamespace(lane, candidate) == second.userNamespace(lane, candidate) {
				t.Fatalf("different projection keys linked %s/%s", lane, candidate.CaseID)
			}
		}
	}
}

func TestGatePopulationsCannotContaminateOrdinaryOrOtherIntervention(t *testing.T) {
	t.Parallel()
	coordinator := newCoordinator(t, coordinatorConfig(3))
	inference, embedding := responders(t, 3)
	scores := map[Lane]float64{LaneOrdinary: 0.9, LaneInference: 0.2, LaneEmbedding: 0.6}
	runner := caseRunnerFunc(func(_ context.Context, request RunRequest) (CaseRunResult, error) {
		if err := useSynthetic(request); err != nil {
			return CaseRunResult{}, err
		}
		return CaseRunResult{Score: scores[request.Lane]}, nil
	})
	report, err := coordinator.Coordinate(context.Background(), eligiblePopulation(10), runner, inference, embedding)
	if err != nil {
		t.Fatal(err)
	}
	if report.Ordinary.SyntheticUsage != (Usage{}) {
		t.Fatalf("ordinary lane contains synthetic accounting: %+v", report.Ordinary.SyntheticUsage)
	}
	if report.InferenceIntervention.Observation != ObservationChanged || report.EmbeddingIntervention.Observation != ObservationChanged {
		t.Fatalf("changed lanes not classified explicitly: inference=%s embedding=%s", report.InferenceIntervention.Observation, report.EmbeddingIntervention.Observation)
	}
	if report.InferenceIntervention.SyntheticUsage.Intervention != InterventionInference ||
		report.EmbeddingIntervention.SyntheticUsage.Intervention != InterventionEmbedding {
		t.Fatalf("synthetic accounting crossed lanes: %+v", report)
	}
	inferencePopulation, err := report.GatePopulation(InterventionInference)
	if err != nil {
		t.Fatal(err)
	}
	embeddingPopulation, err := report.GatePopulation(InterventionEmbedding)
	if err != nil {
		t.Fatal(err)
	}
	for index := 0; index < 3; index++ {
		if inferencePopulation.Baseline[index].Score != 0.9 || inferencePopulation.Ablated[index].Score != 0.2 ||
			embeddingPopulation.Baseline[index].Score != 0.9 || embeddingPopulation.Ablated[index].Score != 0.6 {
			t.Fatalf("gate population contamination at %d: inference=%+v embedding=%+v", index, inferencePopulation, embeddingPopulation)
		}
		if inferencePopulation.Baseline[index].CaseID != inferencePopulation.Ablated[index].CaseID ||
			embeddingPopulation.Baseline[index].CaseID != embeddingPopulation.Ablated[index].CaseID {
			t.Fatalf("unpaired case identity at %d", index)
		}
	}
	inferencePopulation.Baseline[0].Score = 0
	if report.Ordinary.Scores[0].Score != 0.9 {
		t.Fatal("returned gate population aliases ordinary report storage")
	}
	if _, err := report.GatePopulation(InterventionNone); err == nil {
		t.Fatal("ordinary lane accepted as an ablation gate")
	}
}

func TestZeroRelevantCallIsUnavailableNotInvariant(t *testing.T) {
	t.Parallel()
	coordinator := newCoordinator(t, coordinatorConfig(3))
	inference, embedding := responders(t, 3)
	runner := caseRunnerFunc(func(_ context.Context, request RunRequest) (CaseRunResult, error) {
		if request.Lane != LaneInference {
			if err := useSynthetic(request); err != nil {
				return CaseRunResult{}, err
			}
		}
		return CaseRunResult{Score: 0.5}, nil
	})
	report, err := coordinator.Coordinate(context.Background(), eligiblePopulation(8), runner, inference, embedding)
	if err != nil {
		t.Fatal(err)
	}
	got := report.InferenceIntervention
	if got.Observation != ObservationUnavailable || got.UnavailableReason != UnavailableZeroRelevantCalls || got.Complete || got.CompletedCases != 0 {
		t.Fatalf("zero-call lane=%+v", got)
	}
	if _, err := report.GatePopulation(InterventionInference); !errors.Is(err, ErrUnavailablePopulation) {
		t.Fatalf("zero-call population error=%v", err)
	}
	if !report.EmbeddingIntervention.Complete || report.EmbeddingIntervention.Observation != ObservationInvariant {
		t.Fatalf("independent embedding lane was contaminated: %+v", report.EmbeddingIntervention)
	}
}

func TestPartialInterventionFailureDoesNotPublishPartialGatePopulation(t *testing.T) {
	t.Parallel()
	coordinator := newCoordinator(t, coordinatorConfig(4))
	inference, embedding := responders(t, 4)
	var inferenceCalls atomic.Int64
	runner := caseRunnerFunc(func(_ context.Context, request RunRequest) (CaseRunResult, error) {
		if err := useSynthetic(request); err != nil {
			return CaseRunResult{}, err
		}
		if request.Lane == LaneInference && inferenceCalls.Add(1) == 2 {
			return CaseRunResult{}, errors.New("partial intervention failure")
		}
		return CaseRunResult{Score: 0.5}, nil
	})
	report, err := coordinator.Coordinate(context.Background(), eligiblePopulation(9), runner, inference, embedding)
	if err != nil {
		t.Fatal(err)
	}
	got := report.InferenceIntervention
	if got.Observation != ObservationUnavailable || got.UnavailableReason != UnavailablePartialIntervention || got.Complete ||
		got.CompletedCases != 1 || got.Scores != nil || got.SyntheticUsage.ChatApplied != 4 {
		t.Fatalf("partial inference lane=%+v", got)
	}
	if _, err := report.GatePopulation(InterventionInference); !errors.Is(err, ErrUnavailablePopulation) {
		t.Fatalf("partial population error=%v", err)
	}
	if !report.EmbeddingIntervention.Complete {
		t.Fatalf("embedding lane did not remain independent: %+v", report.EmbeddingIntervention)
	}
}

func TestPartialOrdinaryFailureSkipsBothSyntheticPopulations(t *testing.T) {
	t.Parallel()
	coordinator := newCoordinator(t, coordinatorConfig(3))
	inference, embedding := responders(t, 3)
	var ordinaryCalls atomic.Int64
	var interventionCalls atomic.Int64
	runner := caseRunnerFunc(func(_ context.Context, request RunRequest) (CaseRunResult, error) {
		if request.Lane != LaneOrdinary {
			interventionCalls.Add(1)
			return CaseRunResult{}, errors.New("unexpected intervention")
		}
		if ordinaryCalls.Add(1) == 2 {
			return CaseRunResult{}, errors.New("ordinary failure")
		}
		return CaseRunResult{Score: 0.5}, nil
	})
	report, err := coordinator.Coordinate(context.Background(), eligiblePopulation(6), runner, inference, embedding)
	if err != nil {
		t.Fatal(err)
	}
	if report.Ordinary.Complete || report.Ordinary.Scores != nil || report.Ordinary.CompletedCases != 1 {
		t.Fatalf("partial ordinary report=%+v", report.Ordinary)
	}
	if interventionCalls.Load() != 0 {
		t.Fatalf("intervention runner called %d times", interventionCalls.Load())
	}
	for _, lane := range []LaneReport{report.InferenceIntervention, report.EmbeddingIntervention} {
		if lane.UnavailableReason != UnavailableOrdinary || lane.SyntheticUsage.affectedCalls() != 0 {
			t.Fatalf("synthetic lane contaminated after ordinary failure: %+v", lane)
		}
	}
}

func TestHostileRetryLoopIsCappedAndUnavailable(t *testing.T) {
	t.Parallel()
	config := coordinatorConfig(2)
	config.MaxAttempts = maximumAttemptsPerCase
	config.MaxRequests = 2 * 3 * maximumAttemptsPerCase
	refreshCoordinatorProfile(&config)
	config.FrozenProfile.InferenceBudget = inferenceBudget(12, 4096)
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
	var hostileCalls atomic.Int64
	runner := caseRunnerFunc(func(_ context.Context, request RunRequest) (CaseRunResult, error) {
		if err := useSynthetic(request); err != nil {
			return CaseRunResult{}, err
		}
		if request.Lane == LaneInference {
			hostileCalls.Add(1)
			return CaseRunResult{}, MarkRetryable(errors.New("retry forever"))
		}
		return CaseRunResult{Score: 0.5}, nil
	})
	report, err := coordinator.Coordinate(context.Background(), eligiblePopulation(6), runner, inference, embedding)
	if err != nil {
		t.Fatal(err)
	}
	got := report.InferenceIntervention
	if hostileCalls.Load() != maximumAttemptsPerCase || got.AttemptCount != maximumAttemptsPerCase ||
		got.Observation != ObservationUnavailable || got.UnavailableReason != UnavailableRetryExhausted ||
		got.SyntheticUsage.ChatApplied != minimumRelevantCallsPerCase*maximumAttemptsPerCase {
		t.Fatalf("hostile calls=%d lane=%+v", hostileCalls.Load(), got)
	}
	if !report.EmbeddingIntervention.Complete {
		t.Fatalf("embedding lane contaminated by inference retry loop: %+v", report.EmbeddingIntervention)
	}
}

func TestSyntheticBudgetExhaustionOverridesRetryFailure(t *testing.T) {
	t.Parallel()
	config := coordinatorConfig(1)
	config.FrozenProfile.InferenceBudget = inferenceBudget(1, 1024)
	profileSHA256, err := FrozenProfileSHA256(config.FrozenProfile)
	if err != nil {
		t.Fatal(err)
	}
	config.ProfileSHA256 = profileSHA256
	coordinator := newCoordinator(t, config)
	inference, err := NewResponder(InterventionInference, inferenceBudget(1, 1024))
	if err != nil {
		t.Fatal(err)
	}
	_, embedding := responders(t, 1)
	runner := caseRunnerFunc(func(_ context.Context, request RunRequest) (CaseRunResult, error) {
		if err := useSynthetic(request); err != nil {
			return CaseRunResult{}, err
		}
		if request.Lane == LaneInference {
			return CaseRunResult{}, MarkRetryable(errors.New("retry after synthetic call"))
		}
		return CaseRunResult{Score: 0.5}, nil
	})
	report, err := coordinator.Coordinate(context.Background(), eligiblePopulation(3), runner, inference, embedding)
	if err != nil {
		t.Fatal(err)
	}
	got := report.InferenceIntervention
	if got.Observation != ObservationUnavailable || got.UnavailableReason != UnavailableSyntheticBudget ||
		got.SyntheticUsage.ChatAttempts != 2 || got.SyntheticUsage.ChatApplied != 1 ||
		got.SyntheticUsage.RejectedRequests != 1 || !got.SyntheticUsage.BudgetExhausted {
		t.Fatalf("budget-exhausted lane=%+v", got)
	}
}

func TestGlobalRequestCapStopsRetryAmplification(t *testing.T) {
	t.Parallel()
	config := coordinatorConfig(2)
	config.MaxRequests = 6
	refreshCoordinatorProfile(&config)
	coordinator := newCoordinator(t, config)
	inference, embedding := responders(t, 2)
	var retried atomic.Bool
	runner := caseRunnerFunc(func(_ context.Context, request RunRequest) (CaseRunResult, error) {
		if request.Lane == LaneOrdinary && !retried.Swap(true) {
			return CaseRunResult{}, MarkRetryable(errors.New("one retry"))
		}
		if err := useSynthetic(request); err != nil {
			return CaseRunResult{}, err
		}
		return CaseRunResult{Score: 0.5}, nil
	})
	report, err := coordinator.Coordinate(context.Background(), eligiblePopulation(5), runner, inference, embedding)
	if err != nil {
		t.Fatal(err)
	}
	if !report.Ordinary.Complete || !report.InferenceIntervention.Complete {
		t.Fatalf("earlier lanes unexpectedly incomplete: %+v", report)
	}
	if report.EmbeddingIntervention.Observation != ObservationUnavailable ||
		report.EmbeddingIntervention.UnavailableReason != UnavailableRequestLimit ||
		report.EmbeddingIntervention.CompletedCases != 1 {
		t.Fatalf("global request cap report=%+v", report.EmbeddingIntervention)
	}
}

func TestCancellationAndNonCooperativeDeadlineAreBounded(t *testing.T) {
	t.Run("parent cancellation", func(t *testing.T) {
		config := coordinatorConfig(1)
		config.RequestTimeout = time.Second
		refreshCoordinatorProfile(&config)
		coordinator := newCoordinator(t, config)
		inference, embedding := responders(t, 1)
		started := make(chan struct{})
		var once sync.Once
		runner := caseRunnerFunc(func(ctx context.Context, _ RunRequest) (CaseRunResult, error) {
			once.Do(func() { close(started) })
			<-ctx.Done()
			return CaseRunResult{}, ctx.Err()
		})
		ctx, cancel := context.WithCancel(context.Background())
		result := make(chan CoordinationReport, 1)
		go func() {
			report, err := coordinator.Coordinate(ctx, eligiblePopulation(2), runner, inference, embedding)
			if err != nil {
				t.Errorf("Coordinate: %v", err)
			}
			result <- report
		}()
		<-started
		cancel()
		report := <-result
		if report.Ordinary.Observation != ObservationUnavailable || report.Ordinary.UnavailableReason != UnavailableCancelled {
			t.Fatalf("cancelled ordinary lane=%+v", report.Ordinary)
		}
		if report.InferenceIntervention.UnavailableReason != UnavailableOrdinary || report.EmbeddingIntervention.UnavailableReason != UnavailableOrdinary {
			t.Fatalf("ordinary cancellation did not stop interventions: %+v", report)
		}
	})

	t.Run("runner ignores request context", func(t *testing.T) {
		config := coordinatorConfig(1)
		config.RequestTimeout = 15 * time.Millisecond
		config.TotalTimeout = 100 * time.Millisecond
		refreshCoordinatorProfile(&config)
		coordinator := newCoordinator(t, config)
		inference, embedding := responders(t, 1)
		release := make(chan struct{})
		runner := caseRunnerFunc(func(context.Context, RunRequest) (CaseRunResult, error) {
			<-release
			return CaseRunResult{Score: 0.5}, nil
		})
		started := time.Now()
		report, err := coordinator.Coordinate(context.Background(), eligiblePopulation(2), runner, inference, embedding)
		elapsed := time.Since(started)
		close(release)
		if err != nil {
			t.Fatal(err)
		}
		if elapsed > 250*time.Millisecond {
			t.Fatalf("non-cooperative runner escaped time cap: %s", elapsed)
		}
		if report.Ordinary.UnavailableReason != UnavailableDeadline {
			t.Fatalf("deadline ordinary lane=%+v", report.Ordinary)
		}
	})
}

func TestAttemptResponderCapabilityIsRevokedAfterReturn(t *testing.T) {
	t.Parallel()
	coordinator := newCoordinator(t, coordinatorConfig(1))
	inference, embedding := responders(t, 1)
	var retained SyntheticResponder
	runner := caseRunnerFunc(func(_ context.Context, request RunRequest) (CaseRunResult, error) {
		if err := useSynthetic(request); err != nil {
			return CaseRunResult{}, err
		}
		if request.Lane == LaneInference {
			retained = request.Responder
		}
		return CaseRunResult{Score: 0.5}, nil
	})
	_, err := coordinator.Coordinate(context.Background(), eligiblePopulation(3), runner, inference, embedding)
	if err != nil {
		t.Fatal(err)
	}
	before := inference.Snapshot()
	if _, err := retained.Chat("openai/gpt-oss-20b", 1); !errors.Is(err, context.Canceled) {
		t.Fatalf("retained capability error=%v", err)
	}
	after := inference.Snapshot()
	if !reflect.DeepEqual(before, after) {
		t.Fatalf("revoked capability changed usage: before=%+v after=%+v", before, after)
	}
}

func TestCoordinatorRejectsContaminatedOrCrossLaneResponders(t *testing.T) {
	t.Parallel()
	coordinator := newCoordinator(t, coordinatorConfig(1))
	runner := caseRunnerFunc(func(context.Context, RunRequest) (CaseRunResult, error) {
		return CaseRunResult{Score: 0.5}, nil
	})
	inference, embedding := responders(t, 1)
	if _, err := coordinator.Coordinate(context.Background(), eligiblePopulation(2), runner, nil, embedding); err == nil {
		t.Fatal("missing inference responder accepted")
	}
	if _, err := coordinator.Coordinate(context.Background(), eligiblePopulation(2), runner, embedding, inference); err == nil {
		t.Fatal("cross-lane responders accepted")
	}
	inference, embedding = responders(t, 1)
	if _, err := inference.Chat("model", 1); err != nil {
		t.Fatal(err)
	}
	if _, err := coordinator.Coordinate(context.Background(), eligiblePopulation(2), runner, inference, embedding); err == nil {
		t.Fatal("responder with pre-existing activity accepted")
	}
}

func TestConcurrentCoordinatesAreRaceFreeAndIndependent(t *testing.T) {
	const runs = 32
	coordinator := newCoordinator(t, coordinatorConfig(2))
	population := eligiblePopulation(8)
	runner := caseRunnerFunc(func(_ context.Context, request RunRequest) (CaseRunResult, error) {
		if err := useSynthetic(request); err != nil {
			return CaseRunResult{}, err
		}
		return CaseRunResult{Score: 0.5}, nil
	})
	var wait sync.WaitGroup
	wait.Add(runs)
	for range runs {
		go func() {
			defer wait.Done()
			inference, embedding := responders(t, 2)
			report, err := coordinator.Coordinate(context.Background(), population, runner, inference, embedding)
			if err != nil {
				t.Errorf("Coordinate: %v", err)
				return
			}
			if !report.Ordinary.Complete || !report.InferenceIntervention.Complete || !report.EmbeddingIntervention.Complete {
				t.Errorf("incomplete concurrent report: %+v", report)
			}
		}()
	}
	wait.Wait()
}

func TestRunnerPanicAndInvalidScoreBecomeUnavailableEvidence(t *testing.T) {
	for _, testCase := range []struct {
		name   string
		runner CaseRunner
	}{
		{"panic", caseRunnerFunc(func(context.Context, RunRequest) (CaseRunResult, error) { panic("hostile") })},
		{"nan", caseRunnerFunc(func(context.Context, RunRequest) (CaseRunResult, error) { return CaseRunResult{Score: math.NaN()}, nil })},
		{"infinite", caseRunnerFunc(func(context.Context, RunRequest) (CaseRunResult, error) {
			return CaseRunResult{Score: math.Inf(1)}, nil
		})},
		{"above one", caseRunnerFunc(func(context.Context, RunRequest) (CaseRunResult, error) { return CaseRunResult{Score: 1.1}, nil })},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			coordinator := newCoordinator(t, coordinatorConfig(1))
			inference, embedding := responders(t, 1)
			report, err := coordinator.Coordinate(context.Background(), eligiblePopulation(2), testCase.runner, inference, embedding)
			if err != nil {
				t.Fatal(err)
			}
			if report.Ordinary.Observation != ObservationUnavailable || report.Ordinary.UnavailableReason != UnavailableCaseFailure {
				t.Fatalf("invalid runner report=%+v", report.Ordinary)
			}
		})
	}
}

func FuzzCoordinatorSelectionOrderIndependence(f *testing.F) {
	f.Add([]byte("seed population"), uint8(5))
	f.Add([]byte{0, 1, 2, 3, 4, 5}, uint8(2))
	f.Fuzz(func(t *testing.T, data []byte, requested uint8) {
		size := int(requested%16) + 1
		config := coordinatorConfig(size)
		coordinator := newCoordinator(t, config)
		dataDigest := sha256.Sum256(data)
		population := EligiblePopulation{BenchVersion: 9, Confirmation: true, Cases: make([]EligibleCase, size+3)}
		for index := range population.Cases {
			population.Cases[index] = EligibleCase{
				CaseID: fmt.Sprintf("case-%03d-%x", index, dataDigest),
				UserID: fmt.Sprintf("user-%03d-%x", index, dataDigest),
			}
		}
		first, firstDigest, err := coordinator.selectCases(population)
		if err != nil {
			t.Fatal(err)
		}
		sort.Slice(population.Cases, func(left, right int) bool { return population.Cases[left].CaseID > population.Cases[right].CaseID })
		second, secondDigest, err := coordinator.selectCases(population)
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(first, second) || firstDigest != secondDigest {
			t.Fatalf("selection depends on population order: %v/%v %s/%s", first, second, firstDigest, secondDigest)
		}
	})
}

func FuzzProjectionSeparatesKeysLanesAndCases(f *testing.F) {
	f.Add([]byte("case"), []byte("user"), []byte("projection material"))
	f.Fuzz(func(t *testing.T, caseData, userData, keyData []byte) {
		candidate := EligibleCase{CaseID: fmt.Sprintf("case-%x", caseData), UserID: fmt.Sprintf("user-%x", userData)}
		firstKey := append([]byte("first-projection-key-32-bytes----"), keyData...)
		secondKey := append([]byte("second-projection-key-32-bytes---"), keyData...)
		first := &Coordinator{projectionKey: firstKey}
		second := &Coordinator{projectionKey: secondKey}
		ordinary := first.userNamespace(LaneOrdinary, candidate)
		inference := first.userNamespace(LaneInference, candidate)
		embedding := first.userNamespace(LaneEmbedding, candidate)
		if ordinary == inference || ordinary == embedding || inference == embedding {
			t.Fatal("lane-separated namespaces collided")
		}
		if ordinary == second.userNamespace(LaneOrdinary, candidate) {
			t.Fatal("different projection keys collided")
		}
	})
}

func FuzzEmbeddingProjectionNormalizedReplay(f *testing.F) {
	f.Add([]byte("semantic   text"), []byte("projection material"))
	f.Add([]byte("line one\r\nline two"), []byte{0, 1, 2, 3})
	f.Fuzz(func(t *testing.T, inputData, keyData []byte) {
		if len(inputData) > 1024 {
			inputData = inputData[:1024]
		}
		normalized, err := normalizeEmbeddingInput(string(inputData))
		if err != nil {
			return
		}
		key := sha256.Sum256(keyData)
		first, err := syntheticEmbeddingResponse([]string{string(inputData)}, key[:])
		if err != nil {
			t.Fatal(err)
		}
		replayed, err := syntheticEmbeddingResponse([]string{" \t" + normalized + "\r\n"}, key[:])
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(first.Embeddings, replayed.Embeddings) {
			t.Fatal("normalized embedding replay drifted")
		}
	})
}
