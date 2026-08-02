// Package refharness holds the deterministic, no-LLM reference routing policy:
// for a prompt it picks the single catalog tool whose name+description keywords
// best overlap the prompt (or nothing). It is a pure function of (prompt,
// tools) — the calibration harness relies on that determinism so the only thing
// varying between runs is the dataset (i.e. dataset difficulty). Shared by
// cmd/refharness (the HTTP server) and cmd/benchcal (the offline calibrator).
//
// CALIBRATION TRUST NOTE: this router has NO model and ZERO run-to-run variance.
// It is the right instrument for isolating dataset difficulty, but it is the
// WRONG instrument for setting any variance/sigma gate: a real locked-model
// reasoning harness has materially higher variance, so a gate tuned against this
// lookup table will certify a deterministic parser as a stable champion.
// Recalibrate variance gates against a real locked-model harness's measured
// spread. See docs/calibration-trust.md.
package refharness

import (
	"encoding/json"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// MinOverlap is the keyword-overlap floor to call a tool at all; below it the
// harness calls nothing (so no-tool / abstention prompts can score correctly).
const MinOverlap = 1

var stopwords = map[string]bool{
	"the": true, "a": true, "an": true, "to": true, "of": true, "for": true,
	"and": true, "or": true, "in": true, "on": true, "at": true, "is": true,
	"it": true, "with": true, "your": true, "you": true, "me": true, "my": true,
	"can": true, "please": true, "i": true, "that": true, "this": true,
	"what": true, "whats": true, "how": true, "do": true, "does": true,
}

// Route picks the single best-overlapping tool for the prompt, or nothing. It is
// a pure function of (prompt, tools): deterministic and order-stable.
func Route(prompt string, tools []protocol.ToolDefinition) []protocol.ObservedToolCall {
	promptTokens := tokenSet(prompt)

	best := -1
	bestScore := 0
	for i, t := range tools {
		score := overlap(promptTokens, signal(t))
		if score > bestScore {
			bestScore = score
			best = i
		}
	}

	if best < 0 || bestScore < MinOverlap {
		return []protocol.ObservedToolCall{} // call nothing
	}
	return []protocol.ObservedToolCall{{Name: tools[best].Name, Args: json.RawMessage(`{}`)}}
}

// signal is the keyword set a tool routes on: its name (split on '_') plus the
// content words of its description.
func signal(t protocol.ToolDefinition) map[string]bool {
	s := tokenSet(strings.ReplaceAll(t.Name, "_", " "))
	for tok := range tokenSet(t.Description) {
		s[tok] = true
	}
	return s
}

func overlap(a, b map[string]bool) int {
	small, large := a, b
	if len(b) < len(a) {
		small, large = b, a
	}
	n := 0
	for tok := range small {
		if large[tok] {
			n++
		}
	}
	return n
}

// tokenSet lowercases, splits on non-alphanumerics, and drops stopwords + tokens
// shorter than 3 chars.
func tokenSet(s string) map[string]bool {
	out := map[string]bool{}
	for _, f := range strings.FieldsFunc(strings.ToLower(s), func(r rune) bool {
		return !(r >= 'a' && r <= 'z') && !(r >= '0' && r <= '9')
	}) {
		if len(f) < 3 || stopwords[f] {
			continue
		}
		out[f] = true
	}
	return out
}
