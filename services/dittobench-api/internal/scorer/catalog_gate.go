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
//	    the model was in a position to act -- at least one attributed completion
//	    left a non-memory tool CHOOSABLE (offered and not suppressed by
//	    tool_choice) -- unless the safe harbor applies;
//	(b) expected_tool_not_offered: a case whose expected non-memory tool was never
//	    offered on any attributed completion earns nothing -- unless the safe
//	    harbor applies, or the validator executed that very tool under matched
//	    v10 provenance (the model demonstrably chose it, so it was offered);
//	(c) swallowed_model_call: on a no-tool case, a model-emitted non-memory call
//	    the harness never executed is a host override of the model's choice;
//	(d) the PUBLISHED semantic-preloading safe harbor: trimming is free when the
//	    retained catalog contains the top-k (CatalogSafeHarborTopK) tools of the
//	    published embedding (CatalogSemanticTopK) for the request, or when the
//	    catalog is merely non-empty on a declarative/chit-chat/decline case.
//
// Rules (a) and (b) are evaluated over the UNION of the case's attributed
// completions, not only the deciding turn: an offer on any turn the model
// declined is still model-chosen restraint, and an expected tool offered on any
// turn was choosable. CatalogEvidence.Completions keeps the per-turn record
// (AfterLastToolResult) for the audit trail.
//
// Posture. The gate ships SHADOW by default: findings and the run-level
// catalog_suppression_rate are recorded, no score moves. ENFORCE zeroes the
// case's tool credit in scored scope, and only on SETTLED evidence -- an
// unattributable, truncated, or unparseable capture fails OPEN -- whose
// attribution does not rest on an uncorroborated X-Ditto-Case-Id claim. An
// incomplete case whose every candidate completion offered an actionable
// catalog (CatalogPresentLowerBound) records the safe harbor without settling.
// Flipping to enforce is an
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
	// CatalogFindingMemoryOnlyCatalog: the choosable catalog held only memory
	// tools, so the model was not in a position to act.
	CatalogFindingMemoryOnlyCatalog = "memory_only_catalog"
	// CatalogFindingOfferInferredFromExecution: rule (b) waived because the
	// validator executed the expected tool under matched v10 provenance.
	CatalogFindingOfferInferredFromExecution = "offer_inferred_from_execution"
	// CatalogFindingCatalogPresentLowerBound: the case is unattributed, but every
	// completion that could have served it offered an actionable catalog.
	CatalogFindingCatalogPresentLowerBound = "catalog_present_lower_bound"
	// CatalogFindingClaimUncorroborated: enforce withheld a settled zero because
	// the case's attribution rests on an uncorroborated X-Ditto-Case-Id claim.
	CatalogFindingClaimUncorroborated = "claim_attribution_uncorroborated"
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
	// other field is meaningful only when it is, except LowerBound.
	Settled bool
	// LowerBound is true when the evidence was NOT settled but
	// CatalogPresentLowerBound held on a case whose restraint the non-empty
	// safe harbor covers; the case fails open with the harbor recorded.
	LowerBound bool
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
// validator-observed executed trajectory, and executionProven reports whether
// that trajectory carries matched v10 provenance (every execution was
// model-emitted), which is what lets an executed expected tool waive rule (b).
func EvaluateCatalogGate(
	c protocol.ToolCase,
	catalog []protocol.ToolDefinition,
	evidence *protocol.CatalogEvidence,
	observed []protocol.ObservedToolCall,
	executionProven bool,
) CatalogGateVerdict {
	var verdict CatalogGateVerdict
	if evidence == nil {
		verdict.Findings = []string{CatalogFindingEvidenceUnavailable}
		return verdict
	}
	_, tempting := catalogTemptingClassCategories[c.Category]
	if evidence.CompletionsTotal == nil || !evidence.Complete {
		verdict.Findings = []string{CatalogFindingEvidenceIncomplete}
		// Sound lower bound under concurrency: every completion that could have
		// served this case offered an actionable catalog, so on a case the
		// non-empty safe harbor covers the model was in a position to act
		// whichever completions were really its own. Never settles, never zeroes.
		if evidence.CatalogPresentLowerBound && len(c.ExpectedTools) == 0 && !tempting {
			verdict.LowerBound = true
			verdict.SafeHarbor = CatalogSafeHarborNonEmptyDeclarative
			verdict.Findings = append(verdict.Findings, CatalogFindingCatalogPresentLowerBound, CatalogFindingSafeHarbor)
		}
		return verdict
	}
	verdict.Settled = true
	verdict.NoCompletion = *evidence.CompletionsTotal == 0
	verdict.CatalogAbsent = !verdict.NoCompletion && !evidence.CatalogPresent
	verdict.TopK = CatalogSemanticTopK(c.Prompt, catalog, CatalogSafeHarborTopK)
	// ToolsOffered is already the CHOOSABLE union: the relay drops every tool a
	// tool_choice of "none" suppressed and keeps only the pinned tool under
	// "tool:<name>". A catalog of memory tools alone leaves nothing to act on.
	offered := make(map[string]struct{}, len(evidence.ToolsOffered))
	actionable := false
	for _, tool := range evidence.ToolsOffered {
		offered[tool.Name] = struct{}{}
		if !memoryTools[tool.Name] {
			actionable = true
		}
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
	if evidence.CatalogPresent && !actionable {
		verdict.Findings = append(verdict.Findings, CatalogFindingMemoryOnlyCatalog)
	}

	if len(c.ExpectedTools) == 0 {
		// (c) symmetric provenance: restraint is scored on what the MODEL chose.
		if swallowed := swallowedModelCalls(evidence.ModelEmittedToolCalls, observed); len(swallowed) > 0 {
			verdict.Findings = append(verdict.Findings, CatalogFindingSwallowedModelCall)
			verdict.Zero = true
		}
		// (a) restraint requires an actionable offer, unless the safe harbor
		// applies. tool_choice "none" or a memory-only catalog is host-decided
		// restraint: the model was never in a position to act.
		switch {
		case retainedTopK:
			verdict.SafeHarbor = CatalogSafeHarborSemanticTopK
		case actionable && !tempting:
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
	// they are not required on the wire. A tool the validator EXECUTED under
	// matched v10 provenance was model-emitted, hence offered, even when the
	// request body that offered it could not be parsed: the offer is inferred
	// from the execution rather than charged as withheld.
	executed := make(map[string]struct{}, len(observed))
	if executionProven {
		for _, call := range observed {
			executed[call.Name] = struct{}{}
		}
	}
	var missing []string
	inferred := false
	for _, spec := range c.ExpectedTools {
		if memoryTools[spec.Name] {
			continue
		}
		if _, ok := offered[spec.Name]; ok {
			continue
		}
		if _, ok := executed[spec.Name]; ok {
			inferred = true
			continue
		}
		missing = append(missing, spec.Name)
	}
	if inferred {
		verdict.Findings = append(verdict.Findings, CatalogFindingOfferInferredFromExecution)
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

// catalogExecutionProven reports whether the case's v10 provenance proves that
// every observed execution was model-emitted: the ledger is complete, nothing
// executed was unmatched, and the matched count equals the observed trajectory.
func catalogExecutionProven(provenance *protocol.ToolProvenanceEvidence, observed []protocol.ObservedToolCall) bool {
	return provenance != nil && provenance.Complete && provenance.Unmatched == 0 &&
		len(observed) > 0 && provenance.Matched == len(observed)
}

// catalogClaimsCorroborated reports whether a settled zero may rest on this
// evidence: every completion booked on an X-Ditto-Case-Id claim emitted a tool
// call the validator consumed for the same case. A claim is harness-asserted
// and only membership-checked, so a zero that rests on an uncorroborated claim
// is withheld under enforce and recorded instead.
func catalogClaimsCorroborated(evidence *protocol.CatalogEvidence) bool {
	return evidence.ClaimAttributedCompletions == evidence.ClaimCorroboratedCompletions
}

// ApplyCatalogGateForVersion attaches the catalog evidence and verdict to a
// tool case's score and, under CatalogGateEnforce in ScopeScored on settled,
// claim-corroborated evidence, zeroes its tool credit. Below
// CatalogGateBenchVersion it returns cs untouched, so every frozen contract
// keeps its bytes. Memory-kind cases are never gated here (their tool half is
// memory routing). cs.ToolProvenance (v10) decides whether the observed
// trajectory may waive rule (b).
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
	verdict := EvaluateCatalogGate(c, catalog, evidence, observed, catalogExecutionProven(cs.ToolProvenance, observed))
	attached := &protocol.CatalogEvidence{}
	if evidence != nil {
		copied := *evidence
		copied.Findings = append([]string(nil), evidence.Findings...)
		attached = &copied
	}
	for _, finding := range verdict.Findings {
		attached.Findings = appendDistinct(attached.Findings, finding)
	}
	enforcing := verdict.Settled && verdict.Zero && posture == CatalogGateEnforce && scope == ScopeScored
	corroborated := evidence != nil && catalogClaimsCorroborated(evidence)
	if enforcing && !corroborated {
		attached.Findings = appendDistinct(attached.Findings, CatalogFindingClaimUncorroborated)
	}
	zero := enforcing && corroborated
	if zero {
		attached.Findings = appendDistinct(attached.Findings, CatalogFindingZeroed)
		cs.ToolScore = 0
		cs.Notes = append(cs.Notes, fmt.Sprintf(
			"v13 catalog gate (enforce): %s; tool credit zeroed",
			strings.Join(verdict.Findings, ", "),
		))
	} else if enforcing {
		cs.Notes = append(cs.Notes, fmt.Sprintf(
			"v13 catalog gate (enforce): %s; attribution rests on %d uncorroborated X-Ditto-Case-Id claim(s), recorded only",
			strings.Join(verdict.Findings, ", "),
			evidence.ClaimAttributedCompletions-evidence.ClaimCorroboratedCompletions,
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
