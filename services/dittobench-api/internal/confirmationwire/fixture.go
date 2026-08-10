// Package confirmationwire builds the cross-language Bench v9 evidence fixture.
//
// The fixture is constructed through the production LongMemEval and ablation
// APIs.  It is intentionally small, deterministic, and free of credentials.
package confirmationwire

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/ablation"
	"github.com/ditto-assistant/dittobench-api/internal/longmemeval"
)

const (
	artifactSHA256          = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	longMemDatasetSHA256    = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	ablationDatasetSHA256   = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
	thresholdManifestSHA256 = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
)

// DimensionFixture keeps the producer's canonical digest beside the exact
// evidence object. LatencyMS is transport metadata for dimensions whose Go
// evidence contract does not carry latency itself.
type DimensionFixture[T any] struct {
	GoEvidenceSHA256 string `json:"go_evidence_sha256"`
	LatencyMS        uint64 `json:"latency_ms"`
	Evidence         T      `json:"evidence"`
}

// Fixture is the committed Go-to-Platform contract vector.
type Fixture struct {
	FixtureSchema     string                                 `json:"fixture_schema"`
	LongMemEval       DimensionFixture[longmemeval.Evidence] `json:"longmemeval"`
	InferenceAblation DimensionFixture[ablation.Evidence]    `json:"inference_ablation"`
	EmbeddingAblation DimensionFixture[ablation.Evidence]    `json:"embedding_ablation"`
}

// BuildFixture constructs genuine validated producer evidence and serializes
// it with encoding/json, matching what the Go service puts on the wire.
func BuildFixture() (Fixture, error) {
	longMem, longMemDigest, err := buildLongMemEvidence()
	if err != nil {
		return Fixture{}, err
	}
	inference, embedding, err := buildAblationEvidence()
	if err != nil {
		return Fixture{}, err
	}
	return Fixture{
		FixtureSchema: "dittobench-v9-confirmation-go-evidence-v1",
		LongMemEval: DimensionFixture[longmemeval.Evidence]{
			GoEvidenceSHA256: longMemDigest,
			LatencyMS:        longMem.LatencyMS,
			Evidence:         longMem,
		},
		InferenceAblation: DimensionFixture[ablation.Evidence]{
			GoEvidenceSHA256: inference.SHA256,
			LatencyMS:        111,
			Evidence:         inference.Evidence,
		},
		EmbeddingAblation: DimensionFixture[ablation.Evidence]{
			GoEvidenceSHA256: embedding.SHA256,
			LatencyMS:        222,
			Evidence:         embedding.Evidence,
		},
	}, nil
}

// MarshalFixture returns a newline-terminated, indented fixture for stable
// review diffs. Evidence digests still cover each producer's compact canonical
// JSON, not this presentation wrapper.
func MarshalFixture() ([]byte, error) {
	fixture, err := BuildFixture()
	if err != nil {
		return nil, err
	}
	raw, err := json.MarshalIndent(fixture, "", "  ")
	if err != nil {
		return nil, err
	}
	return append(raw, '\n'), nil
}

func buildLongMemEvidence() (longmemeval.Evidence, string, error) {
	profile := longmemeval.Profile{
		SchemaVersion:      longmemeval.ProfileSchemaVersion,
		Revision:           "longmem-launch-v1",
		BenchVersion:       9,
		DatasetRevision:    "longmemeval-s-2026-08-08",
		DatasetSHA256:      longMemDatasetSHA256,
		SelectorRevision:   longmemeval.SelectorRevisionV1,
		SelectionSeed:      387,
		CasesPerCapability: 2,
		Providers: []longmemeval.ProviderPolicy{{
			Lane:                "judge",
			Provider:            "openrouter",
			ProfileRevision:     "longmem-judge-v1",
			Model:               "openai/gpt-oss-20b",
			MaxRequests:         20,
			MaxPromptTokens:     2_000,
			MaxCompletionTokens: 400,
			MaxTotalTokens:      2_400,
			MaxCostUSDmicros:    50_000,
		}},
	}
	cases := []longmemeval.Case{
		{QuestionID: "extraction-a", QuestionType: "single-session-user"},
		{QuestionID: "extraction-b", QuestionType: "single-session-assistant"},
		{QuestionID: "multi-a", QuestionType: "multi-session"},
		{QuestionID: "multi-b", QuestionType: "multi-session"},
		{QuestionID: "temporal-a", QuestionType: "temporal-reasoning"},
		{QuestionID: "temporal-b", QuestionType: "temporal-reasoning"},
		{QuestionID: "knowledge-a", QuestionType: "knowledge-update"},
		{QuestionID: "knowledge-b", QuestionType: "knowledge-update"},
		{QuestionID: "preference-a", QuestionType: "single-session-preference"},
		{QuestionID: "preference-b", QuestionType: "single-session-preference"},
		{QuestionID: "abstention-a_abs", QuestionType: "single-session-user"},
		{QuestionID: "abstention-b_abs", QuestionType: "single-session-user"},
	}
	selection, err := longmemeval.Select(profile, cases)
	if err != nil {
		return longmemeval.Evidence{}, "", fmt.Errorf("select LongMem fixture: %w", err)
	}
	outcomes := make([]longmemeval.Outcome, len(selection.Cases))
	for index, selected := range selection.Cases {
		outcomes[index] = longmemeval.Outcome{
			QuestionID: selected.QuestionID,
			Correct:    index%2 == 0,
		}
	}
	score, err := longmemeval.Aggregate(selection, outcomes)
	if err != nil {
		return longmemeval.Evidence{}, "", fmt.Errorf("aggregate LongMem fixture: %w", err)
	}
	evidence, err := longmemeval.NewEvidence(
		profile,
		selection,
		artifactSHA256,
		4_321,
		score,
		[]longmemeval.ProviderEvidence{{
			Lane:              "judge",
			CostSource:        longmemeval.AuthoritativeCostV1,
			Currency:          "USD",
			Provider:          "openrouter",
			ProfileRevision:   "longmem-judge-v1",
			Model:             "openai/gpt-oss-20b",
			FallbackUsed:      false,
			Requests:          12,
			Successes:         12,
			ReceiptedRequests: 12,
			PromptTokens:      1_200,
			CompletionTokens:  120,
			TotalTokens:       1_320,
			CostUSDmicros:     12_345,
			ReceiptSetSHA256:  "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
		}},
	)
	if err != nil {
		return longmemeval.Evidence{}, "", fmt.Errorf("construct LongMem fixture: %w", err)
	}
	digest, err := evidence.Digest(profile, selection)
	if err != nil {
		return longmemeval.Evidence{}, "", fmt.Errorf("digest LongMem fixture: %w", err)
	}
	return evidence, digest, nil
}

type fixtureRunner struct{}

func (fixtureRunner) RunCase(
	_ context.Context,
	request ablation.RunRequest,
) (ablation.CaseRunResult, error) {
	switch request.Lane {
	case ablation.LaneOrdinary:
		if request.Responder != nil {
			return ablation.CaseRunResult{}, fmt.Errorf("ordinary fixture lane received responder")
		}
		return ablation.CaseRunResult{Score: 0.9}, nil
	case ablation.LaneInference:
		if request.Responder == nil {
			return ablation.CaseRunResult{}, fmt.Errorf("inference fixture lane lacks responder")
		}
		if _, err := request.Responder.Chat("openai/gpt-oss-20b", 16); err != nil {
			return ablation.CaseRunResult{}, err
		}
		if _, err := request.Responder.Chat("openai/gpt-oss-20b", 24); err != nil {
			return ablation.CaseRunResult{}, err
		}
		return ablation.CaseRunResult{Score: 0.8}, nil
	case ablation.LaneEmbedding:
		if request.Responder == nil {
			return ablation.CaseRunResult{}, fmt.Errorf("embedding fixture lane lacks responder")
		}
		if _, err := request.Responder.Embeddings([]string{"bounded text"}); err != nil {
			return ablation.CaseRunResult{}, err
		}
		if _, err := request.Responder.Embeddings([]string{"independent bounded text"}); err != nil {
			return ablation.CaseRunResult{}, err
		}
		return ablation.CaseRunResult{Score: 0.8}, nil
	default:
		return ablation.CaseRunResult{}, fmt.Errorf("unexpected fixture lane %q", request.Lane)
	}
}

func buildAblationEvidence() (ablation.Evaluation, ablation.Evaluation, error) {
	selectionKey := []byte("selection-key-32-bytes-long-fixed!")
	projectionKey := []byte("projection-key-32-bytes-long-fixed")
	policy := ablation.CoordinatorPolicy{
		SampleSize:                 2,
		MaxAttempts:                1,
		MaxRequests:                6,
		RequestTimeoutMilliseconds: 1_000,
		TotalTimeoutMilliseconds:   5_000,
	}
	inferenceBudget := ablation.Budget{MaxChatRequests: 4, MaxChatInputBytes: 256}
	embeddingBudget := ablation.Budget{
		MaxEmbeddingRequests:   4,
		MaxEmbeddingInputs:     4,
		MaxEmbeddingInputBytes: 256,
	}
	profile := ablation.FrozenProfile{
		ContractVersion:         ablation.ProfileContractVersion,
		Revision:                "ablation-launch-v1",
		BenchVersion:            ablation.BenchVersionV9,
		DatasetSHA256:           ablationDatasetSHA256,
		ThresholdManifestSHA256: thresholdManifestSHA256,
		SelectionKeySHA256:      bytesSHA256(selectionKey),
		ProjectionKeySHA256:     bytesSHA256(projectionKey),
		CoordinatorPolicy:       policy,
		InferenceBudget:         inferenceBudget,
		EmbeddingBudget:         embeddingBudget,
		InferenceThreshold:      0.2,
		EmbeddingThreshold:      0.2,
	}
	profileSHA256, err := ablation.FrozenProfileSHA256(profile)
	if err != nil {
		return ablation.Evaluation{}, ablation.Evaluation{}, fmt.Errorf("digest ablation profile: %w", err)
	}
	coordinator, err := ablation.NewCoordinator(ablation.CoordinatorConfig{
		SampleSize:     policy.SampleSize,
		MaxAttempts:    policy.MaxAttempts,
		MaxRequests:    policy.MaxRequests,
		RequestTimeout: time.Duration(policy.RequestTimeoutMilliseconds) * time.Millisecond,
		TotalTimeout:   time.Duration(policy.TotalTimeoutMilliseconds) * time.Millisecond,
		ArtifactSHA256: artifactSHA256,
		SelectionKey:   selectionKey,
		ProjectionKey:  projectionKey,
		FrozenProfile:  profile,
		ProfileSHA256:  profileSHA256,
	})
	if err != nil {
		return ablation.Evaluation{}, ablation.Evaluation{}, fmt.Errorf("construct ablation coordinator: %w", err)
	}
	inferenceResponder, err := ablation.NewResponder(ablation.InterventionInference, inferenceBudget)
	if err != nil {
		return ablation.Evaluation{}, ablation.Evaluation{}, err
	}
	embeddingResponder, err := ablation.NewResponder(ablation.InterventionEmbedding, embeddingBudget)
	if err != nil {
		return ablation.Evaluation{}, ablation.Evaluation{}, err
	}
	report, err := coordinator.Coordinate(
		context.Background(),
		ablation.EligiblePopulation{
			BenchVersion: ablation.BenchVersionV9,
			Confirmation: true,
			Cases: []ablation.EligibleCase{
				{CaseID: "case-a", UserID: "private-user-a"},
				{CaseID: "case-b", UserID: "private-user-b"},
			},
		},
		fixtureRunner{},
		inferenceResponder,
		embeddingResponder,
	)
	if err != nil {
		return ablation.Evaluation{}, ablation.Evaluation{}, fmt.Errorf("coordinate ablation fixture: %w", err)
	}
	evaluate := func(intervention ablation.Intervention, threshold float64) (ablation.Evaluation, error) {
		population, gateErr := report.GatePopulation(intervention)
		if gateErr != nil {
			return ablation.Evaluation{}, gateErr
		}
		return ablation.Evaluate(ablation.EvaluateInput{
			BenchVersion:            ablation.BenchVersionV9,
			ArtifactSHA256:          artifactSHA256,
			Intervention:            intervention,
			Mode:                    ablation.ModeShadow,
			ProfileRevision:         profile.Revision,
			ThresholdManifestSHA256: profile.ThresholdManifestSHA256,
			DatasetSHA256:           profile.DatasetSHA256,
			Threshold:               threshold,
			Baseline:                population.Baseline,
			Ablated:                 population.Ablated,
			Usage:                   population.Usage,
			FrozenProfile:           profile,
			AblationProfileSHA256:   profileSHA256,
			Coordinator:             report,
		})
	}
	inference, err := evaluate(ablation.InterventionInference, profile.InferenceThreshold)
	if err != nil {
		return ablation.Evaluation{}, ablation.Evaluation{}, fmt.Errorf("evaluate inference fixture: %w", err)
	}
	embedding, err := evaluate(ablation.InterventionEmbedding, profile.EmbeddingThreshold)
	if err != nil {
		return ablation.Evaluation{}, ablation.Evaluation{}, fmt.Errorf("evaluate embedding fixture: %w", err)
	}
	return inference, embedding, nil
}

func bytesSHA256(value []byte) string {
	digest := sha256.Sum256(value)
	return hex.EncodeToString(digest[:])
}
