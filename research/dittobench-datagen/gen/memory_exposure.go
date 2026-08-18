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

// AuditV10MemoryExposure evaluates only cases with explicit evidence bindings.
// Missing evidence is an error: silently treating an unresolvable answer as a
// transformation would make the difficulty gate pass for the wrong reason.
func AuditV10MemoryExposure(artifact DatasetArtifact) (MemoryExposureResult, error) {
	if artifact.BenchVersion != protocol.BenchVersionV10 {
		return MemoryExposureResult{}, fmt.Errorf("memory exposure audit requires bench version 10, got %d", artifact.BenchVersion)
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
		if containsWholeAnswer(evidence.String(), memoryCase.ExpectedAnswer) {
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
