package longmemeval

import (
	"errors"
	"fmt"
	"math"
)

type Outcome struct {
	QuestionID string
	Correct    bool
}

type CapabilityScore struct {
	Capability Capability `json:"capability"`
	Correct    int        `json:"correct"`
	Count      int        `json:"count"`
	Mean       float64    `json:"mean"`
}

type Score struct {
	LongMemMean   float64           `json:"longmem_mean"`
	LongMemStdErr float64           `json:"longmem_stderr"`
	CaseCount     int               `json:"case_count"`
	PerCapability []CapabilityScore `json:"per_capability"`
}

// Aggregate requires exactly one judged outcome for every selected case. The
// mean gives each capability equal weight. Its standard error is the ordinary
// stratified standard error of that equal-weight macro mean.
func Aggregate(selection Selection, outcomes []Outcome) (Score, error) {
	if len(selection.Cases) == 0 || !validSHA256(selection.CaseSetDigest) ||
		selection.CaseSetDigest != caseSetDigest(selection) {
		return Score{}, errors.New("selection is empty or lacks a valid case_set_digest")
	}
	selected := make(map[string]Capability, len(selection.Cases))
	for _, item := range selection.Cases {
		if _, exists := selected[item.QuestionID]; exists {
			return Score{}, fmt.Errorf("selection repeats question_id %q", item.QuestionID)
		}
		selected[item.QuestionID] = item.Capability
	}

	correct := make(map[Capability]int, len(capabilityOrder))
	counts := make(map[Capability]int, len(capabilityOrder))
	seen := make(map[string]struct{}, len(outcomes))
	for _, outcome := range outcomes {
		capability, ok := selected[outcome.QuestionID]
		if !ok {
			return Score{}, fmt.Errorf("outcome contains unexpected question_id %q", outcome.QuestionID)
		}
		if _, duplicate := seen[outcome.QuestionID]; duplicate {
			return Score{}, fmt.Errorf("outcome repeats question_id %q", outcome.QuestionID)
		}
		seen[outcome.QuestionID] = struct{}{}
		counts[capability]++
		if outcome.Correct {
			correct[capability]++
		}
	}
	if len(seen) != len(selected) {
		return Score{}, fmt.Errorf("outcomes cover %d of %d selected cases", len(seen), len(selected))
	}

	result := Score{CaseCount: len(selection.Cases)}
	var macroMean, macroVariance float64
	for _, capability := range capabilityOrder {
		count := counts[capability]
		if count < 2 {
			return Score{}, fmt.Errorf("capability %q has %d outcomes; at least 2 required", capability, count)
		}
		mean := float64(correct[capability]) / float64(count)
		result.PerCapability = append(result.PerCapability, CapabilityScore{
			Capability: capability,
			Correct:    correct[capability],
			Count:      count,
			Mean:       round6(mean),
		})
		macroMean += mean / float64(len(capabilityOrder))
		// Unbiased Bernoulli sample variance, then variance of this stratum's
		// mean, then the square of its equal macro weight.
		sampleVariance := mean * (1 - mean) * float64(count) / float64(count-1)
		macroVariance += (sampleVariance / float64(count)) /
			float64(len(capabilityOrder)*len(capabilityOrder))
	}
	result.LongMemMean = round6(macroMean)
	result.LongMemStdErr = round6(math.Sqrt(macroVariance))
	return result, nil
}

func round6(value float64) float64 {
	return math.Round(value*1e6) / 1e6
}
