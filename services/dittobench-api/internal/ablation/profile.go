package ablation

import (
	"fmt"
	"math"
)

const ProfileContractVersion = "dittobench-v9-ablation-profile-v1"

// CoordinatorPolicy is the frozen, non-secret execution policy. Selection and
// projection keys are intentionally excluded; their effects are committed by
// the coordinator and selected-population digests.
type CoordinatorPolicy struct {
	SampleSize                 int   `json:"sample_size"`
	MaxAttempts                int   `json:"max_attempts"`
	MaxRequests                int   `json:"max_requests"`
	RequestTimeoutMilliseconds int64 `json:"request_timeout_milliseconds"`
	TotalTimeoutMilliseconds   int64 `json:"total_timeout_milliseconds"`
}

// FrozenProfile binds the artifact-independent public policy shared by all
// ablation runs. The legacy-named key digests are public profile commitments,
// not hashes of mounted runtime secrets; the scorer derives per-bundle material
// from the signed lease. The runtime artifact remains a per-run coordinator and
// evaluation input. Threshold values remain in the separately checksummed
// threshold manifest.
type FrozenProfile struct {
	ContractVersion         string            `json:"contract_version"`
	Revision                string            `json:"revision"`
	BenchVersion            int               `json:"bench_version"`
	DatasetSHA256           string            `json:"dataset_sha256"`
	ThresholdManifestSHA256 string            `json:"threshold_manifest_sha256"`
	SelectionKeySHA256      string            `json:"selection_key_sha256"`
	ProjectionKeySHA256     string            `json:"projection_key_sha256"`
	CoordinatorPolicy       CoordinatorPolicy `json:"coordinator_policy"`
	InferenceBudget         Budget            `json:"inference_budget"`
	EmbeddingBudget         Budget            `json:"embedding_budget"`
	InferenceThreshold      float64           `json:"inference_threshold"`
	EmbeddingThreshold      float64           `json:"embedding_threshold"`
}

func (p CoordinatorPolicy) validate() error {
	if p.SampleSize <= 0 || p.SampleSize > maximumPairedCases {
		return fmt.Errorf("invalid profile paired sample size")
	}
	if p.MaxAttempts != 1 {
		return fmt.Errorf("profile maximum attempts must be one")
	}
	minimumRequests := p.SampleSize * 3
	maximumUsefulRequests := minimumRequests * p.MaxAttempts
	if p.MaxRequests < minimumRequests || p.MaxRequests > maximumCoordinatorRuns || p.MaxRequests > maximumUsefulRequests {
		return fmt.Errorf("invalid profile coordinator request cap")
	}
	if p.RequestTimeoutMilliseconds <= 0 || p.RequestTimeoutMilliseconds > maximumRequestTimeout.Milliseconds() {
		return fmt.Errorf("invalid profile request timeout")
	}
	if p.TotalTimeoutMilliseconds <= 0 || p.TotalTimeoutMilliseconds > maximumTotalTimeout.Milliseconds() ||
		p.TotalTimeoutMilliseconds < p.RequestTimeoutMilliseconds {
		return fmt.Errorf("invalid profile total timeout")
	}
	return nil
}

func (p FrozenProfile) validate() error {
	if p.ContractVersion != ProfileContractVersion || !ConfirmationBenchVersionSupported(p.BenchVersion) {
		return fmt.Errorf("invalid frozen ablation profile contract")
	}
	if !validateRevision(p.Revision) {
		return fmt.Errorf("invalid frozen ablation profile revision")
	}
	for name, value := range map[string]string{
		"dataset": p.DatasetSHA256, "threshold manifest": p.ThresholdManifestSHA256,
		"selection key":  p.SelectionKeySHA256,
		"projection key": p.ProjectionKeySHA256,
	} {
		if !canonicalSHA256(value) {
			return fmt.Errorf("invalid frozen ablation %s digest", name)
		}
	}
	if err := p.CoordinatorPolicy.validate(); err != nil {
		return err
	}
	if err := p.InferenceBudget.validate(InterventionInference); err != nil {
		return fmt.Errorf("invalid frozen inference budget: %w", err)
	}
	if err := p.EmbeddingBudget.validate(InterventionEmbedding); err != nil {
		return fmt.Errorf("invalid frozen embedding budget: %w", err)
	}
	for name, threshold := range map[string]float64{
		"inference": p.InferenceThreshold, "embedding": p.EmbeddingThreshold,
	} {
		if math.IsNaN(threshold) || math.IsInf(threshold, 0) || threshold < 0 || threshold > 1 {
			return fmt.Errorf("invalid frozen %s threshold", name)
		}
	}
	return nil
}

func FrozenProfileSHA256(profile FrozenProfile) (string, error) {
	if err := profile.validate(); err != nil {
		return "", err
	}
	digest, _, err := digestJSON(profile)
	return digest, err
}
