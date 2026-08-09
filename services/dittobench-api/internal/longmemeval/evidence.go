package longmemeval

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"reflect"
	"sort"
	"strings"
)

// ProviderEvidence is trusted accounting copied from provider receipts. A
// caller must not synthesize CostUSDmicros from model prices.
type ProviderEvidence struct {
	Lane              string `json:"lane"`
	CostSource        string `json:"cost_source"`
	Currency          string `json:"currency"`
	Provider          string `json:"provider"`
	ProfileRevision   string `json:"profile_revision"`
	Model             string `json:"model"`
	FallbackUsed      bool   `json:"fallback_used"`
	Requests          uint64 `json:"requests"`
	Successes         uint64 `json:"successes"`
	ReceiptedRequests uint64 `json:"receipted_requests"`
	PromptTokens      uint64 `json:"prompt_tokens"`
	CompletionTokens  uint64 `json:"completion_tokens"`
	TotalTokens       uint64 `json:"total_tokens"`
	CostUSDmicros     uint64 `json:"cost_usd_micros"`
	ReceiptSetSHA256  string `json:"receipt_set_sha256"`
}

// ValidateProviderEvidence checks identity, completeness, and all hard caps.
// It rejects fallback and non-receipt cost even if the numeric spend is small.
func ValidateProviderEvidence(policy ProviderPolicy, evidence ProviderEvidence) error {
	if err := policy.validate(); err != nil {
		return err
	}
	if evidence.Lane != policy.Lane || evidence.Provider != policy.Provider ||
		evidence.ProfileRevision != policy.ProfileRevision || evidence.Model != policy.Model {
		return fmt.Errorf("provider identity drift on lane %q", policy.Lane)
	}
	if evidence.FallbackUsed {
		return fmt.Errorf("provider fallback is forbidden on lane %q", policy.Lane)
	}
	if evidence.CostSource != AuthoritativeCostV1 || evidence.Currency != "USD" {
		return fmt.Errorf("lane %q lacks authoritative USD provider receipt cost", policy.Lane)
	}
	if !validSHA256(evidence.ReceiptSetSHA256) {
		return fmt.Errorf("lane %q receipt_set_sha256 is invalid", policy.Lane)
	}
	if evidence.Requests == 0 || evidence.Successes == 0 || evidence.Successes > evidence.Requests {
		return fmt.Errorf("lane %q has invalid request/success counts", policy.Lane)
	}
	if evidence.ReceiptedRequests != evidence.Requests {
		return fmt.Errorf("lane %q does not have a receipt for every provider request", policy.Lane)
	}
	totalTokens, ok := addUint64(evidence.PromptTokens, evidence.CompletionTokens)
	if !ok || evidence.TotalTokens != totalTokens {
		return fmt.Errorf("lane %q token totals are inconsistent", policy.Lane)
	}
	if evidence.Requests > policy.MaxRequests || evidence.PromptTokens > policy.MaxPromptTokens ||
		evidence.CompletionTokens > policy.MaxCompletionTokens || evidence.TotalTokens > policy.MaxTotalTokens ||
		evidence.CostUSDmicros > policy.MaxCostUSDmicros {
		return fmt.Errorf("lane %q exceeds its frozen provider budget", policy.Lane)
	}
	return nil
}

func addUint64(left, right uint64) (uint64, bool) {
	if ^uint64(0)-left < right {
		return 0, false
	}
	return left + right, true
}

// Evidence is the canonical dimension result. ArtifactSHA256 binds reuse to
// bytes rather than a filename. Profile and case-set digests bind every other
// frozen input without exposing private harness projection aliases.
type Evidence struct {
	SchemaVersion    int                `json:"schema_version"`
	ArtifactSHA256   string             `json:"artifact_sha256"`
	BenchVersion     int                `json:"bench_version"`
	ProfileChecksum  string             `json:"profile_checksum"`
	CaseSetDigest    string             `json:"case_set_digest"`
	DatasetRevision  string             `json:"dataset_revision"`
	DatasetSHA256    string             `json:"dataset_sha256"`
	LatencyMS        uint64             `json:"latency_ms"`
	Score            Score              `json:"score"`
	ProviderEvidence []ProviderEvidence `json:"provider_evidence"`
}

// NewEvidence validates every seam and returns normalized evidence. Filename is
// intentionally absent: renamed identical artifacts produce identical bytes.
func NewEvidence(
	profile Profile,
	selection Selection,
	artifactSHA256 string,
	latencyMS uint64,
	score Score,
	providerEvidence []ProviderEvidence,
) (Evidence, error) {
	profileChecksum, err := profile.Checksum()
	if err != nil {
		return Evidence{}, err
	}
	if !validSHA256(artifactSHA256) {
		return Evidence{}, errors.New("artifact_sha256 must be 64 lowercase hexadecimal characters")
	}
	if latencyMS == 0 {
		return Evidence{}, errors.New("latency_ms must be positive and validator-observed")
	}
	if selection.ProfileChecksum != profileChecksum || selection.DatasetSHA256 != profile.DatasetSHA256 ||
		selection.CaseSetDigest != caseSetDigest(selection) {
		return Evidence{}, errors.New("selection does not match the frozen profile or its case-set digest")
	}
	if err := validateScore(selection, score); err != nil {
		return Evidence{}, err
	}

	policies := make(map[string]ProviderPolicy, len(profile.Providers))
	for _, policy := range profile.Providers {
		policies[policy.Lane] = policy
	}
	normalizedProviders := append([]ProviderEvidence(nil), providerEvidence...)
	sort.Slice(normalizedProviders, func(i, j int) bool {
		return normalizedProviders[i].Lane < normalizedProviders[j].Lane
	})
	seen := make(map[string]struct{}, len(normalizedProviders))
	for _, observed := range normalizedProviders {
		policy, ok := policies[observed.Lane]
		if !ok {
			return Evidence{}, fmt.Errorf("unexpected provider lane %q", observed.Lane)
		}
		if _, duplicate := seen[observed.Lane]; duplicate {
			return Evidence{}, fmt.Errorf("duplicate provider lane %q", observed.Lane)
		}
		seen[observed.Lane] = struct{}{}
		if err := ValidateProviderEvidence(policy, observed); err != nil {
			return Evidence{}, err
		}
	}
	if len(seen) != len(policies) {
		return Evidence{}, errors.New("provider evidence does not cover every frozen lane")
	}

	return Evidence{
		SchemaVersion:    EvidenceSchemaVersion,
		ArtifactSHA256:   artifactSHA256,
		BenchVersion:     profile.BenchVersion,
		ProfileChecksum:  profileChecksum,
		CaseSetDigest:    selection.CaseSetDigest,
		DatasetRevision:  profile.DatasetRevision,
		DatasetSHA256:    profile.DatasetSHA256,
		LatencyMS:        latencyMS,
		Score:            score,
		ProviderEvidence: normalizedProviders,
	}, nil
}

func validateScore(selection Selection, score Score) error {
	if score.CaseCount != len(selection.Cases) || len(score.PerCapability) != len(capabilityOrder) {
		return errors.New("score cardinality does not match selection")
	}
	expectedCounts := make(map[Capability]int, len(capabilityOrder))
	for _, item := range selection.Cases {
		expectedCounts[item.Capability]++
	}
	var macroMean, macroVariance float64
	for index, capability := range capabilityOrder {
		item := score.PerCapability[index]
		if item.Capability != capability || item.Count < 2 || item.Correct < 0 || item.Correct > item.Count {
			return fmt.Errorf("invalid score for capability %q", capability)
		}
		if item.Count != expectedCounts[capability] {
			return fmt.Errorf("capability %q count does not match selection", capability)
		}
		mean := float64(item.Correct) / float64(item.Count)
		if item.Mean != round6(mean) {
			return fmt.Errorf("capability %q mean does not match counts", capability)
		}
		macroMean += mean / float64(len(capabilityOrder))
		sampleVariance := mean * (1 - mean) * float64(item.Count) / float64(item.Count-1)
		macroVariance += (sampleVariance / float64(item.Count)) /
			float64(len(capabilityOrder)*len(capabilityOrder))
	}
	if score.LongMemMean != round6(macroMean) || score.LongMemStdErr != round6(math.Sqrt(macroVariance)) {
		return errors.New("longmem mean or uncertainty does not match capability counts")
	}
	return nil
}

// Validate replays construction against the frozen profile and private
// selection. Mutating any score, provider accounting, or identity after
// NewEvidence therefore makes the evidence ineligible for signing.
func (e Evidence) Validate(profile Profile, selection Selection) error {
	_, err := e.normalized(profile, selection)
	return err
}

func (e Evidence) normalized(profile Profile, selection Selection) (Evidence, error) {
	if e.SchemaVersion != EvidenceSchemaVersion || e.BenchVersion != 9 ||
		!validSHA256(e.ArtifactSHA256) || !validSHA256(e.ProfileChecksum) ||
		!validSHA256(e.CaseSetDigest) || !validSHA256(e.DatasetSHA256) ||
		strings.TrimSpace(e.DatasetRevision) == "" {
		return Evidence{}, errors.New("evidence identity is incomplete")
	}
	if math.IsNaN(e.Score.LongMemMean) || math.IsInf(e.Score.LongMemMean, 0) ||
		math.IsNaN(e.Score.LongMemStdErr) || math.IsInf(e.Score.LongMemStdErr, 0) {
		return Evidence{}, errors.New("evidence score contains a non-finite value")
	}
	validated, err := NewEvidence(
		profile, selection, e.ArtifactSHA256, e.LatencyMS, e.Score, e.ProviderEvidence,
	)
	if err != nil {
		return Evidence{}, err
	}
	normalizedInput := e
	normalizedInput.ProviderEvidence = append([]ProviderEvidence(nil), e.ProviderEvidence...)
	sort.Slice(normalizedInput.ProviderEvidence, func(i, j int) bool {
		return normalizedInput.ProviderEvidence[i].Lane < normalizedInput.ProviderEvidence[j].Lane
	})
	if !reflect.DeepEqual(normalizedInput, validated) {
		return Evidence{}, errors.New("evidence fields contradict the frozen profile or normalized result")
	}
	return validated, nil
}

// Digest hashes canonical JSON only after replaying full validation. This is
// the digest later signing code must call; malformed mutated evidence cannot
// accidentally become signable.
func (e Evidence) Digest(profile Profile, selection Selection) (string, error) {
	normalized, err := e.normalized(profile, selection)
	if err != nil {
		return "", err
	}
	raw, err := json.Marshal(normalized)
	if err != nil {
		return "", err
	}
	return digestBytes(raw), nil
}
