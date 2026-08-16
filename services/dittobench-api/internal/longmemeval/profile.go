package longmemeval

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
)

const (
	ProfileSchemaVersion     = 1
	EvidenceSchemaVersion    = 2
	SelectorRevisionV1       = "longmemeval-s-stratified-sha256-v1"
	AuthoritativeCostV1      = "provider_receipt_v1"
	V10MinCasesPerCapability = 8
	V10MinHistorySessions    = 55
	V10MinHistoryBytes       = 400_000
)

// Capability is one of the six disjoint LongMemEval confirmation strata.
type Capability string

const (
	CapabilityExtraction            Capability = "extraction"
	CapabilityMultiSessionReasoning Capability = "multi_session_reasoning"
	CapabilityTemporalReasoning     Capability = "temporal_reasoning"
	CapabilityKnowledgeUpdate       Capability = "knowledge_update"
	CapabilityPreference            Capability = "preference"
	CapabilityAbstention            Capability = "abstention"
)

var capabilityOrder = []Capability{
	CapabilityExtraction,
	CapabilityMultiSessionReasoning,
	CapabilityTemporalReasoning,
	CapabilityKnowledgeUpdate,
	CapabilityPreference,
	CapabilityAbstention,
}

// Capabilities returns a copy in the canonical report order.
func Capabilities() []Capability {
	return append([]Capability(nil), capabilityOrder...)
}

// ProviderPolicy freezes one trusted provider lane and its hard bundle caps.
// CostUSDmicros is an integer so accounting never depends on floating point or
// a mutable list-price estimate.
type ProviderPolicy struct {
	Lane                string `json:"lane"`
	Provider            string `json:"provider"`
	ProfileRevision     string `json:"profile_revision"`
	Model               string `json:"model"`
	MaxRequests         uint64 `json:"max_requests"`
	MaxPromptTokens     uint64 `json:"max_prompt_tokens"`
	MaxCompletionTokens uint64 `json:"max_completion_tokens"`
	MaxTotalTokens      uint64 `json:"max_total_tokens"`
	MaxCostUSDmicros    uint64 `json:"max_cost_usd_micros"`
}

// Profile is the complete immutable selection and accounting contract. The
// selection seed belongs to the profile, so the durable evidence key need not
// add a second, hidden seed dimension.
type Profile struct {
	SchemaVersion      int              `json:"schema_version"`
	Revision           string           `json:"revision"`
	BenchVersion       int              `json:"bench_version"`
	DatasetRevision    string           `json:"dataset_revision"`
	DatasetSHA256      string           `json:"dataset_sha256"`
	SelectorRevision   string           `json:"selector_revision"`
	SelectionSeed      uint64           `json:"selection_seed"`
	CasesPerCapability int              `json:"cases_per_capability"`
	MinHistorySessions int              `json:"min_history_sessions,omitempty"`
	MinHistoryBytes    int              `json:"min_history_bytes,omitempty"`
	Providers          []ProviderPolicy `json:"providers"`
}

func (p Profile) Validate() error {
	if p.SchemaVersion != ProfileSchemaVersion {
		return fmt.Errorf("profile schema_version must be %d", ProfileSchemaVersion)
	}
	if strings.TrimSpace(p.Revision) == "" || strings.TrimSpace(p.DatasetRevision) == "" {
		return errors.New("profile revision and dataset revision are required")
	}
	if p.BenchVersion != 9 && p.BenchVersion != 10 && p.BenchVersion != 11 {
		return errors.New("LongMemEval confirmation profile must select bench_version 9, 10, or 11")
	}
	if !validSHA256(p.DatasetSHA256) {
		return errors.New("dataset_sha256 must be 64 lowercase hexadecimal characters")
	}
	if p.SelectorRevision != SelectorRevisionV1 {
		return fmt.Errorf("unsupported selector_revision %q", p.SelectorRevision)
	}
	if p.CasesPerCapability < 2 {
		return errors.New("cases_per_capability must be at least 2 for uncertainty")
	}
	if p.BenchVersion == 9 && (p.MinHistorySessions != 0 || p.MinHistoryBytes != 0) {
		return errors.New("bench_version 9 does not define deep-history floors")
	}
	if p.BenchVersion >= 10 && (p.CasesPerCapability < V10MinCasesPerCapability ||
		p.MinHistorySessions < V10MinHistorySessions || p.MinHistoryBytes < V10MinHistoryBytes) {
		return errors.New("bench_version 10+ profile is below the deep-history case or history floor")
	}
	if len(p.Providers) == 0 {
		return errors.New("at least one provider policy is required")
	}
	seen := make(map[string]struct{}, len(p.Providers))
	for _, policy := range p.Providers {
		if err := policy.validate(); err != nil {
			return err
		}
		if _, ok := seen[policy.Lane]; ok {
			return fmt.Errorf("duplicate provider lane %q", policy.Lane)
		}
		seen[policy.Lane] = struct{}{}
	}
	return nil
}

func (p ProviderPolicy) validate() error {
	if strings.TrimSpace(p.Lane) == "" || strings.TrimSpace(p.Provider) == "" ||
		strings.TrimSpace(p.ProfileRevision) == "" || strings.TrimSpace(p.Model) == "" {
		return errors.New("provider lane, provider, profile revision, and model are required")
	}
	if p.MaxRequests == 0 || p.MaxPromptTokens == 0 || p.MaxCompletionTokens == 0 ||
		p.MaxTotalTokens == 0 || p.MaxCostUSDmicros == 0 {
		return fmt.Errorf("provider lane %q must set positive request, token, and cost caps", p.Lane)
	}
	if p.MaxPromptTokens > p.MaxTotalTokens || p.MaxCompletionTokens > p.MaxTotalTokens {
		return fmt.Errorf("provider lane %q component token cap exceeds total cap", p.Lane)
	}
	if _, ok := addUint64(p.MaxPromptTokens, p.MaxCompletionTokens); !ok {
		return fmt.Errorf("provider lane %q token caps overflow uint64", p.Lane)
	}
	return nil
}

// Checksum returns the SHA-256 of the canonical profile JSON. Provider input
// order does not affect the checksum; lane identity does.
func (p Profile) Checksum() (string, error) {
	if err := p.Validate(); err != nil {
		return "", err
	}
	normalized := p
	normalized.Providers = append([]ProviderPolicy(nil), p.Providers...)
	sort.Slice(normalized.Providers, func(i, j int) bool {
		return normalized.Providers[i].Lane < normalized.Providers[j].Lane
	})
	raw, err := json.Marshal(normalized)
	if err != nil {
		return "", err
	}
	return digestBytes(raw), nil
}

func digestBytes(raw []byte) string {
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:])
}

func validSHA256(value string) bool {
	if len(value) != sha256.Size*2 || strings.ToLower(value) != value {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}
