package toolexec

import (
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v18 tool encounters (protocol.Encounter). A fixture built from a case
// that carries an encounter serves the encounter's argument-keyed mock world
// instead of the single-needle content: every served tool answers the first
// matching rule, or the encounter's fallback. Refusals still come first, so an
// unlinked connector, a snapshot toolkit or a wording decoy is refused inside
// an encounter exactly as anywhere else in the run.

// encounterFallback is served when an encounter has no fallback of its own.
const encounterFallback = "No matching results for that request."

// Encounter returns the case's encounter, or nil.
func (f Fixture) Encounter() *protocol.Encounter { return f.encounter }

// encounterToolResult answers one call of an encounter case.
func encounterToolResult(f Fixture, req protocol.ToolExecRequest, priorSameTool int) protocol.ToolExecResponse {
	if msg, refused := f.unavailable(req.Name, req.Args); refused {
		return protocol.ToolExecResponse{Error: msg}
	}
	if !Serves(req.Name) {
		return protocol.ToolExecResponse{Error: "tool not available via this endpoint: " + req.Name}
	}
	if result, errText, matched := f.encounter.Respond(req.Name, req.Args, priorSameTool); matched {
		if errText != "" {
			return protocol.ToolExecResponse{Error: errText}
		}
		return protocol.ToolExecResponse{Result: result}
	}
	if f.encounter.Fallback != "" {
		return protocol.ToolExecResponse{Result: f.encounter.Fallback}
	}
	return protocol.ToolExecResponse{Result: encounterFallback}
}
