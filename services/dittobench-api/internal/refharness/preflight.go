package refharness

import (
	"encoding/json"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// PreflightPrefix remains reserved for legacy non-scored reachability turns.
// Current validators self-check their listener and do not send this synthetic
// case, but older validators and compatible harnesses still use it.
const PreflightPrefix = "preflight:"

// IsPreflight reports whether the case is a legacy reachability preflight turn.
func IsPreflight(caseID string) bool { return strings.HasPrefix(caseID, PreflightPrefix) }

// PreflightCalls is the reference implementation of the legacy preflight:
// one search_web call, executed through tool_endpoint. Deterministic, so the
// calibration harness's difficulty signal is unaffected.
func PreflightCalls() []protocol.ObservedToolCall {
	return []protocol.ObservedToolCall{{Name: "search_web", Args: json.RawMessage(`{"query":"preflight"}`)}}
}
