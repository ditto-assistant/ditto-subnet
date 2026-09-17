package grade

import (
	"sort"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Public grader-audit bank types (issue #1522). A bank is a frozen, versioned
// fixture set cmd/graderaudit evaluates against one grading policy: generic
// and prompt-only probes that must NOT pass, positive controls that must, hard
// negatives that must score 0, reviewed positives that must score, and
// per-kind exposure limits. Banks are public deterministic measurements of
// grader exposure, not secret probes.

// AuditProbe is one public, deterministic no-retrieval strategy. Fixed probes
// are wholly case-independent; prompt-only probes may interpolate only the
// public question and never receive expected values, items, distractors, case
// ids, seeds, or generated memory.
type AuditProbe struct {
	Name         string
	Fixed        protocol.RunResponse
	PromptPrefix string
	PromptSuffix string
}

// Response renders the probe for a public question.
func (p AuditProbe) Response(question string) protocol.RunResponse {
	if p.PromptPrefix == "" && p.PromptSuffix == "" {
		return p.Fixed
	}
	return protocol.RunResponse{FinalText: p.PromptPrefix + question + p.PromptSuffix}
}

// PromptOnly reports whether the probe uses the public question.
func (p AuditProbe) PromptOnly() bool { return p.PromptPrefix != "" || p.PromptSuffix != "" }

// AuditCase is one synthetic case with its positive control; the bank's probes
// are evaluated against it to measure per-kind exposure.
type AuditCase struct {
	Name     string
	Question string
	Case     protocol.MemoryCase
	Positive protocol.RunResponse
}

// AuditNegative is a hard negative: the response must score 0.
type AuditNegative struct {
	Name     string
	Case     protocol.MemoryCase
	Response protocol.RunResponse
}

// AuditPositive is a reviewed positive: an honest pattern the gates threaten,
// which must score at least MinScore (1 when zero).
type AuditPositive struct {
	Name     string
	Case     protocol.MemoryCase
	Response protocol.RunResponse
	MinScore float64
}

// AuditLimit bounds the worst single strategy's pass share and mean credit for
// one answer kind.
type AuditLimit struct {
	MaxPassShare  float64
	MaxMeanCredit float64
}

// AuditBank is one versioned bank. FloorBenchVersion is the grading-policy
// floor it audits (GradingPolicyFloor); the release gate requires every policy
// floor from BenchVersionV9 upward to own exactly one bank.
type AuditBank struct {
	Version           string
	FloorBenchVersion int
	Probes            []AuditProbe
	Cases             []AuditCase
	Negatives         []AuditNegative
	Positives         []AuditPositive
	Limits            map[string]AuditLimit
	// InteractionOnlyKinds intentionally accept a generic response (their
	// exposure is published explicitly); every other kind is a CLAIM kind and
	// is gated below ClaimKindTargetShare in the generated corpus.
	InteractionOnlyKinds []string
}

// ClaimKindTargetShare is the generated-corpus gate for claim kinds: the share
// of cases a public-question-only or canned strategy can pass must stay
// strictly below it, per kind.
const ClaimKindTargetShare = 0.05

// InteractionOnly reports whether kind is exempt from the per-kind claim gate.
func (b AuditBank) InteractionOnly(kind string) bool {
	for _, k := range b.InteractionOnlyKinds {
		if k == kind {
			return true
		}
	}
	return false
}

// GradingPolicyFloor returns the bench version whose grading policy governs
// v: the greatest policy floor <= v. It is derived from the same switch as
// gradingPolicyForVersion (TestGradingPolicyFloorsMatchSwitch pins them).
func GradingPolicyFloor(v int) int {
	floors := GradingPolicyFloors()
	for i := len(floors) - 1; i >= 0; i-- {
		if v >= floors[i] {
			return floors[i]
		}
	}
	return floors[0]
}

// GradingPolicyFloors lists every bench version that introduced a distinct
// grading policy, ascending. The audit floor (BenchVersionV9) and everything
// above it must own an audit bank.
func GradingPolicyFloors() []int {
	floors := []int{protocol.BenchVersionV2, protocol.BenchVersionV8, protocol.BenchVersionV9, protocol.BenchVersionV12, protocol.BenchVersionV13}
	sort.Ints(floors)
	return floors
}

// AuditBankFloor is the first bench version with a public grader audit bank.
const AuditBankFloor = protocol.BenchVersionV9
