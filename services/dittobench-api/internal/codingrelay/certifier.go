package codingrelay

import (
	"context"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

// CertifierEvidenceAdapter exposes a relay through the existing canary
// certifier evidence port without giving the certifier provider or journal
// authority. It does not own relay revocation.
type CertifierEvidenceAdapter struct {
	relay *Relay
}

// NewCertifierEvidenceAdapter creates an unwired evidence-only adapter.
func NewCertifierEvidenceAdapter(relay *Relay) (*CertifierEvidenceAdapter, error) {
	if relay == nil {
		return nil, ErrInvalidConfig
	}
	return &CertifierEvidenceAdapter{relay: relay}, nil
}

// Evidence verifies every source and task budget field before projection.
func (adapter *CertifierEvidenceAdapter) Evidence(
	ctx context.Context,
	binding codingcertifier.InferenceBinding,
) (codingcontract.ModelEvidence, error) {
	if adapter == nil || adapter.relay == nil {
		return codingcontract.ModelEvidence{}, ErrEvidenceUnavailable
	}
	evidence, err := adapter.relay.Evidence(ctx, EvidenceBinding{
		AttemptID: binding.CertificationID, AgentArtifactSHA256: binding.AgentArtifactSHA256,
		HarnessInstanceID: binding.HarnessInstanceID, TicketID: binding.TicketID,
		CaseID: binding.CaseID, ProfileCapabilityID: binding.ProfileCapabilityID,
		InferenceGrantSHA256: binding.InferenceGrantSHA256, Deadline: binding.Deadline,
		RequestBudget:         binding.RequestBudget,
		PromptTokenBudget:     binding.Budgets.ModelInputTokens,
		CompletionTokenBudget: binding.Budgets.ModelOutputTokens,
	})
	return evidence, err
}

var _ codingcertifier.InferenceEvidenceSource = (*CertifierEvidenceAdapter)(nil)
