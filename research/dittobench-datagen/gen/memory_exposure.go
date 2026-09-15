package gen

import (
	"fmt"
	"strings"
	"unicode"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// MemoryExposureResult measures whether each scored answer can be copied as a
// contiguous token sequence from its declared evidence. It deliberately uses
// per-case evidence rather than the whole run, which would count unrelated
// occurrences of short numbers and names as answer leakage.
type MemoryExposureResult struct {
	Eligible          int
	Verbatim          int
	Transformed       int
	MissingEvidenceID int
	// ComputedByRule counts cases whose answer IS a copyable evidence token but
	// that the bench_version >= 13 correction/join rule
	// (v13ComputedQuestionType) classified as computed anyway. It is always 0
	// below v13. Transformed - ComputedByRule is the strict verbatim-rule share,
	// kept visible so a v13 family regression cannot hide behind the rule.
	ComputedByRule int
}

func (r MemoryExposureResult) VerbatimShare() float64 {
	if r.Eligible == 0 {
		return 0
	}
	return float64(r.Verbatim) / float64(r.Eligible)
}

func (r MemoryExposureResult) TransformedShare() float64 {
	if r.Eligible == 0 {
		return 0
	}
	return float64(r.Transformed) / float64(r.Eligible)
}

// StrictTransformedShare is the share of eligible cases whose answer is not a
// copyable evidence token under the exact v10 verbatim rule, ignoring the v13
// correction/join classification. Equal to TransformedShare below v13.
func (r MemoryExposureResult) StrictTransformedShare() float64 {
	if r.Eligible == 0 {
		return 0
	}
	return float64(r.Transformed-r.ComputedByRule) / float64(r.Eligible)
}

// AuditV10MemoryExposure evaluates a v10 artifact. It is the source-compatible
// entry point behind the original v10 transformation gate; new callers pass an
// explicit version through AuditMemoryExposureForVersion.
func AuditV10MemoryExposure(artifact DatasetArtifact) (MemoryExposureResult, error) {
	return AuditMemoryExposureForVersion(artifact, protocol.BenchVersionV10)
}

// AuditMemoryExposureForVersion evaluates only cases with explicit evidence
// bindings under an explicit contract. The artifact must have been generated
// for benchVersion, which must carry evidence bindings (v10 and later), so an
// audit can never silently re-pin itself to a different contract than the one
// it names. Missing evidence is an error: silently treating an unresolvable
// answer as a transformation would make the difficulty gate pass for the wrong
// reason.
//
// From v13 a case whose answer is a copyable token of its evidence still counts
// as COMPUTED when producing it required applying a correction or following a
// join (v13ComputedQuestionType): "who leads it now" after a lead change, or a
// contact reached through a companion link, is a state resolution even though
// the final name is verbatim somewhere in the records. v10..v12 keep the exact
// verbatim rule so their pinned gate numbers are unchanged.
func AuditMemoryExposureForVersion(artifact DatasetArtifact, benchVersion int) (MemoryExposureResult, error) {
	if benchVersion < protocol.BenchVersionV10 {
		return MemoryExposureResult{}, fmt.Errorf("memory exposure audit requires bench version %d or later, got %d", protocol.BenchVersionV10, benchVersion)
	}
	if artifact.BenchVersion != benchVersion {
		return MemoryExposureResult{}, fmt.Errorf("memory exposure audit requested bench version %d, artifact is %d", benchVersion, artifact.BenchVersion)
	}
	pairs := make(map[string]string)
	for _, tc := range artifact.ToolCases {
		for _, pair := range tc.PrerequisitePairs {
			pairs[pair.PairID] = pair.Prompt + " " + pair.Response
		}
	}
	for _, wave := range artifact.MemoryWaves {
		for _, pair := range wave.Pairs {
			pairs[pair.PairID] = pair.Prompt + " " + pair.Response
		}
	}

	result := MemoryExposureResult{}
	for _, memoryCase := range artifact.MemoryCases {
		if memoryCase.ExpectedAnswer == "" || len(memoryCase.V10EvidencePairIDs) == 0 {
			continue
		}
		result.Eligible++
		var evidence strings.Builder
		for _, pairID := range memoryCase.V10EvidencePairIDs {
			text, ok := pairs[pairID]
			if !ok {
				result.MissingEvidenceID++
				continue
			}
			evidence.WriteByte(' ')
			evidence.WriteString(text)
		}
		verbatim := containsWholeAnswer(evidence.String(), memoryCase.ExpectedAnswer)
		if verbatim && benchVersion >= protocol.BenchVersionV13 && v13ComputedQuestionType(memoryCase.QuestionType) {
			verbatim = false
			result.ComputedByRule++
		}
		if verbatim {
			result.Verbatim++
		} else {
			result.Transformed++
		}
	}
	if result.MissingEvidenceID != 0 {
		return result, fmt.Errorf("memory exposure audit could not resolve %d evidence pair ids", result.MissingEvidenceID)
	}
	return result, nil
}

// v13ComputedQuestionTypes are the shared-world oracle families whose answer is
// a copyable evidence token only after a correction has been applied or a join
// followed: current/previous state after an update chain, a trip leg after an
// itinerary correction, a contact reached through the story's cross-record
// link, and the cross-user isolation read. From v13 the exposure audit counts
// them as computed. Programs, story oracles, and every other family keep the
// verbatim rule.
var v13ComputedQuestionTypes = map[string]bool{
	"world-contact-current":           true,
	"world-contact-previous":          true,
	"world-project-lead-current":      true,
	"world-project-lead-previous":     true,
	"world-trip-current":              true,
	"world-trip-changed-leg-current":  true,
	"world-trip-changed-leg-previous": true,
	"world-story-contact-current":     true,
	"world-isolation-contact-current": true,
}

func v13ComputedQuestionType(questionType string) bool {
	return v13ComputedQuestionTypes[questionType]
}

// AnswerVerbatimInEvidence reports whether answer can be copied as a contiguous,
// token-boundary-aligned sequence out of evidence. It is the per-case primitive
// behind AuditV10MemoryExposure, exported so the runtime scorer can decide, for
// one memory case, whether its expected answer is verbatim-recall (present in the
// case's seeded evidence) or COMPUTED (absent, and therefore in the Bench v12
// answer-stuffing slice). A verbatim-recall answer that a harness legitimately
// retrieves is never in the computed slice, which is what keeps the answer-
// stuffing gate off normal RAG.
func AnswerVerbatimInEvidence(evidence, answer string) bool {
	return containsWholeAnswer(evidence, answer)
}

func containsWholeAnswer(evidence, answer string) bool {
	haystack := []rune(strings.ToLower(strings.Join(strings.Fields(evidence), " ")))
	needle := []rune(strings.ToLower(strings.Join(strings.Fields(answer), " ")))
	if len(needle) == 0 || len(needle) > len(haystack) {
		return false
	}
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if string(haystack[i:i+len(needle)]) != string(needle) {
			continue
		}
		beforeOK := i == 0 || !unicode.IsLetter(haystack[i-1]) && !unicode.IsNumber(haystack[i-1])
		after := i + len(needle)
		afterOK := after == len(haystack) || !unicode.IsLetter(haystack[after]) && !unicode.IsNumber(haystack[after])
		if beforeOK && afterOK {
			return true
		}
	}
	return false
}
