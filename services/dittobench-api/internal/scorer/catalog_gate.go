package scorer

import (
	"fmt"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v13 catalog-present gate. Bench v10 provenance proves that an EXECUTED
// tool call was model-selected; it says nothing about a case where no tool ran.
// Two cheap constructs exploit that gap: withhold the tools[] catalog on a
// request-keyed family so the model cannot act (the host, not the model,
// decided restraint), or offer the catalog, let the model emit the call, and
// swallow it before execution. The relay now records what the harness OFFERED
// (protocol.CatalogEvidence) and this gate scores restraint and expected-tool
// credit against it:
//
//	(a) restraint_without_offer: a no-tool case earns restraint credit only when
//	    the model was in a position to act -- unless the safe harbor applies;
//	(b) expected_tool_not_offered: a case whose expected non-memory tool was never
//	    offered on any attributed completion earns nothing -- unless the safe
//	    harbor applies;
//	(c) swallowed_model_call: on a no-tool case, a model-emitted non-memory call
//	    the harness never executed is a host override of the model's choice;
//	(d) the PUBLISHED semantic-preloading safe harbor: trimming is free when the
//	    retained catalog contains the top-k (CatalogSafeHarborTopK) tools of the
//	    published embedding (CatalogSemanticTopK) for the request, or when the
//	    catalog is merely non-empty on a declarative/chit-chat/decline case.
//
// Posture. The gate ships SHADOW by default: findings and the run-level
// catalog_suppression_rate are recorded, no score moves. ENFORCE zeroes the
// case's tool credit in scored scope, and only on SETTLED evidence -- an
// unattributable or truncated capture fails OPEN. Flipping to enforce is an
// operator decision gated on the fleet precondition (completions_total non-nil
// on >= 99% of cases across >= 3 v13-capable validators); the per-validator
// half of that precondition is published in CatalogGateSummary.
//
// Everything here is gated on bench_version >= CatalogGateBenchVersion so v12
// and earlier regenerate and re-grade byte-identically.

// CatalogGateBenchVersion is the floor from which the catalog gate applies.
const CatalogGateBenchVersion = protocol.BenchVersionV13

// CatalogGatePosture selects how a settled catalog-gate finding affects score.
type CatalogGatePosture string

const (
	// CatalogGateShadow records findings and the suppression rate only. Default.
	CatalogGateShadow CatalogGatePosture = "shadow"
	// CatalogGateEnforce zeroes scored tool credit on a settled finding.
	CatalogGateEnforce CatalogGatePosture = "enforce"
)

// ParseCatalogGatePosture maps an operator string to a posture; anything but
// an exact "enforce" is the safe default, shadow.
func ParseCatalogGatePosture(raw string) CatalogGatePosture {
	if strings.EqualFold(strings.TrimSpace(raw), string(CatalogGateEnforce)) {
		return CatalogGateEnforce
	}
	return CatalogGateShadow
}

// Catalog-gate finding names (appended to CatalogEvidence.Findings).
const (
	CatalogFindingRestraintWithoutOffer  = "restraint_without_offer"
	CatalogFindingExpectedToolNotOffered = "expected_tool_not_offered"
	CatalogFindingSwallowedModelCall     = "swallowed_model_call"
	CatalogFindingSafeHarbor             = "semantic_preloading_safe_harbor"
	CatalogFindingEvidenceUnavailable    = "catalog_evidence_unavailable"
	CatalogFindingEvidenceIncomplete     = "catalog_evidence_incomplete"
	CatalogFindingNoModelCompletion      = "no_model_completion"
	CatalogFindingCatalogAbsent          = "catalog_absent"
	CatalogFindingZeroed                 = "catalog_gate_zeroed"
)

// Safe-harbor grounds (CatalogGateVerdict.SafeHarbor).
const (
	CatalogSafeHarborNonEmptyDeclarative = "nonempty_catalog_on_declarative"
	CatalogSafeHarborSemanticTopK        = "retained_semantic_top_k"
)

// catalogTemptingClassCategories are the no-tool categories whose prompt NAMES
// a tool cue while negating it. Restraint there is only evidence of judgment
// when the cued class was actually offered, so the non-empty-catalog safe
// harbor does not apply; the retained set must contain the semantic top-k.
var catalogTemptingClassCategories = map[string]struct{}{
	"negation_no_tool": {},
}

// CatalogGateVerdict is the settled outcome of the catalog gate for one case.
type CatalogGateVerdict struct {
	// Settled is true when attributed, complete evidence was available; every
	// other field is meaningful only when it is.
	Settled bool
	// NoCompletion is true when the harness sent no attributed completion.
	NoCompletion bool
	// CatalogAbsent is true when the harness made completions but offered no
	// tool on any of them.
	CatalogAbsent bool
	// SafeHarbor names the safe-harbor ground that waived a finding, or "".
	SafeHarbor string
	// Findings are the rule findings that fired; Zero is whether they zero the
	// case under enforce.
	Findings []string
	Zero     bool
	// TopK is the published semantic top-k for the request, for the audit trail.
	TopK []string
}

// EvaluateCatalogGate applies the v13 rules to one tool case. It is a pure
// function of its inputs. catalog is the published catalog for the version
// (the reference the safe harbor is stated against); observed is the
// validator-observed executed trajectory.
func EvaluateCatalogGate(
	c protocol.ToolCase,
	catalog []protocol.ToolDefinition,
	evidence *protocol.CatalogEvidence,
	observed []protocol.ObservedToolCall,
) CatalogGateVerdict {
	var verdict CatalogGateVerdict
	if evidence == nil {
		verdict.Findings = []string{CatalogFindingEvidenceUnavailable}
		return verdict
	}
	if evidence.CompletionsTotal == nil || !evidence.Complete {
		verdict.Findings = []string{CatalogFindingEvidenceIncomplete}
		return verdict
	}
	verdict.Settled = true
	verdict.NoCompletion = *evidence.CompletionsTotal == 0
	verdict.CatalogAbsent = !verdict.NoCompletion && !evidence.CatalogPresent
	verdict.TopK = CatalogSemanticTopK(c.Prompt, catalog, CatalogSafeHarborTopK)
	offered := make(map[string]struct{}, len(evidence.ToolsOffered))
	for _, tool := range evidence.ToolsOffered {
		offered[tool.Name] = struct{}{}
	}
	retainedTopK := len(verdict.TopK) > 0
	for _, name := range verdict.TopK {
		if _, ok := offered[name]; !ok {
			retainedTopK = false
			break
		}
	}
	if verdict.NoCompletion {
		verdict.Findings = append(verdict.Findings, CatalogFindingNoModelCompletion)
	}
	if verdict.CatalogAbsent {
		verdict.Findings = append(verdict.Findings, CatalogFindingCatalogAbsent)
	}

	if len(c.ExpectedTools) == 0 {
		// (c) symmetric provenance: restraint is scored on what the MODEL chose.
		if swallowed := swallowedModelCalls(evidence.ModelEmittedToolCalls, observed); len(swallowed) > 0 {
			verdict.Findings = append(verdict.Findings, CatalogFindingSwallowedModelCall)
			verdict.Zero = true
		}
		// (a) restraint requires an offer, unless the safe harbor applies.
		_, tempting := catalogTemptingClassCategories[c.Category]
		switch {
		case retainedTopK:
			verdict.SafeHarbor = CatalogSafeHarborSemanticTopK
		case evidence.CatalogPresent && !tempting:
			verdict.SafeHarbor = CatalogSafeHarborNonEmptyDeclarative
		default:
			verdict.Findings = append(verdict.Findings, CatalogFindingRestraintWithoutOffer)
			verdict.Zero = true
		}
		if verdict.SafeHarbor != "" {
			verdict.Findings = append(verdict.Findings, CatalogFindingSafeHarbor)
		}
		return verdict
	}

	// (b) an expected non-memory tool that was never offered cannot have been
	// the model's choice. Memory tools are harness-internal and never served, so
	// they are not required on the wire.
	var missing []string
	for _, spec := range c.ExpectedTools {
		if memoryTools[spec.Name] {
			continue
		}
		if _, ok := offered[spec.Name]; !ok {
			missing = append(missing, spec.Name)
		}
	}
	if len(missing) == 0 {
		return verdict
	}
	sort.Strings(missing)
	verdict.Findings = append(verdict.Findings, CatalogFindingExpectedToolNotOffered)
	if retainedTopK {
		verdict.SafeHarbor = CatalogSafeHarborSemanticTopK
		verdict.Findings = append(verdict.Findings, CatalogFindingSafeHarbor)
		return verdict
	}
	verdict.Zero = true
	return verdict
}

// swallowedModelCalls returns the non-memory tool names the model emitted more
// times than the validator observed them executed, sorted.
func swallowedModelCalls(emitted []string, observed []protocol.ObservedToolCall) []string {
	executed := make(map[string]int, len(observed))
	for _, call := range observed {
		executed[call.Name]++
	}
	pending := make(map[string]int)
	for _, name := range emitted {
		if memoryTools[name] {
			continue
		}
		pending[name]++
	}
	var swallowed []string
	for name, count := range pending {
		if count > executed[name] {
			swallowed = append(swallowed, name)
		}
	}
	sort.Strings(swallowed)
	return swallowed
}

// ApplyCatalogGateForVersion attaches the catalog evidence and verdict to a
// tool case's score and, under CatalogGateEnforce in ScopeScored on settled
// evidence, zeroes its tool credit. Below CatalogGateBenchVersion it returns cs
// untouched, so every frozen contract keeps its bytes. Memory-kind cases are
// never gated here (their tool half is memory routing).
func ApplyCatalogGateForVersion(
	benchVersion int,
	scope Scope,
	posture CatalogGatePosture,
	cs protocol.CaseScore,
	c protocol.ToolCase,
	catalog []protocol.ToolDefinition,
	evidence *protocol.CatalogEvidence,
	observed []protocol.ObservedToolCall,
) (protocol.CaseScore, CatalogGateVerdict) {
	if benchVersion < CatalogGateBenchVersion || cs.Kind != protocol.KindTool {
		return cs, CatalogGateVerdict{}
	}
	verdict := EvaluateCatalogGate(c, catalog, evidence, observed)
	attached := &protocol.CatalogEvidence{}
	if evidence != nil {
		copied := *evidence
		copied.Findings = append([]string(nil), evidence.Findings...)
		attached = &copied
	}
	for _, finding := range verdict.Findings {
		attached.Findings = appendDistinct(attached.Findings, finding)
	}
	zero := verdict.Settled && verdict.Zero && posture == CatalogGateEnforce && scope == ScopeScored
	if zero {
		attached.Findings = appendDistinct(attached.Findings, CatalogFindingZeroed)
		cs.ToolScore = 0
		cs.Notes = append(cs.Notes, fmt.Sprintf(
			"v13 catalog gate (enforce): %s; tool credit zeroed",
			strings.Join(verdict.Findings, ", "),
		))
	} else if verdict.Zero {
		cs.Notes = append(cs.Notes, fmt.Sprintf(
			"v13 catalog gate (%s): %s; recorded only",
			posture, strings.Join(verdict.Findings, ", "),
		))
	}
	cs.Catalog = attached
	return cs, verdict
}

func appendDistinct(list []string, value string) []string {
	for _, existing := range list {
		if existing == value {
			return list
		}
	}
	return append(list, value)
}
